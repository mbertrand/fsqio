# coding=utf-8
# Copyright 2015 Foursquare Labs Inc. All Rights Reserved.

from __future__ import (
  absolute_import,
  division,
  generators,
  nested_scopes,
  print_function,
  unicode_literals,
  with_statement,
)

from collections import OrderedDict, defaultdict
import os
import pkgutil

from pants.backend.jvm.targets.jar_dependency import JarDependency
from pants.backend.jvm.targets.jar_library import JarLibrary
from pants.backend.jvm.targets.jarable import Jarable
from pants.backend.jvm.targets.scala_library import ScalaLibrary
from pants.backend.jvm.tasks.jar_publish import JarPublish
from pants.backend.jvm.tasks.jar_task import JarBuilderTask
from pants.base.build_environment import get_buildroot
from pants.base.exceptions import TaskError
from pants.base.generator import Generator, TemplateData
from pants.base.payload import Payload
from pants.build_graph.resources import Resources
from pants.build_graph.target import Target
from pants.ivy.bootstrapper import Bootstrapper
from pants.ivy.ivy import Ivy
from pants.util.dirutil import safe_mkdir, safe_open, safe_rmtree
from pants.util.memo import memoized_property
from twitter.common.collections import OrderedSet


_TEMPLATES_RELPATH = os.path.join('templates')


class PomTarget(Target):
  """An aggregate target, representing a group of targets that are published as one ivy artifact."""

  def __init__(self, prefixes=None, provides=None, *args, **kwargs):
    payload = Payload()
    payload.add_fields({
      'prefixes': prefixes,
      'provides': provides,
    })
    super(PomTarget, self).__init__(payload=payload, *args, **kwargs)

  @property
  def provides(self):
    return self.payload.provides


class PomPublish(JarPublish, JarBuilderTask):
  """Publish jars to a maven repository.

  An example graph:
  ```
  jvm_c -> jvm_a, jvm_b
  jvm_d -> jvm_c, jvm_a
  pom_1 -> jvm_a, jvm_b
  pom_2 -> jvm_c, jvm_d, pom_1
  ```

  Allows groups of Pants targets to be published as a single artifact. In our
  example graph, pom_1 and pom_2 are published with ivy. jvm_a and jvm_b are
  included in the publish jar for pom_1 and jvm_c and jvm_d in pom_2. Note that
  because jvm_c and jvm_d depend on jvm_a and jvm_b, but jvm_a and jvm_b are not
  directly included in pom_2, then pom_2 must depend on pom_1. This is enforced
  by the task.

  Pants uses `Apache Ivy <http://ant.apache.org/ivy/>`_ to publish artifacts to
  Maven-style repositories. Pants performs prerequisite tasks like compiling,
  creating jars, and generating ``pom.xml`` files then invokes Ivy to actually
  publish the artifacts, so publishing is largely configured in
  ``ivysettings.xml``. ``BUILD`` and ``pants.ini`` files primarily provide
  linkage between publishable targets and the Ivy ``resolvers`` used to publish
  them.

  Example usage: ::

     ./pants pom-publish --version=0.0.1 src/jvm/io/fsq::
  """

  @classmethod
  def register_options(cls, register):
    super(PomPublish, cls).register_options(register)
    register('--version',
             help='Version to publish jars at.')

  @classmethod
  def prepare(cls, options, round_manager):
    super(PomPublish, cls).prepare(options, round_manager)
    round_manager.require('jars')

  @property
  def version(self):
    if not self.get_options().version:
      raise TaskError("--version is required")
    return self.get_options().version

  @memoized_property
  def get_repos(self):
    if self.get_options().local:
      local_repo = dict(
        resolver='publish_local',
        path=os.path.abspath(os.path.expanduser(self.get_options().local)),
        confs=['default'],
        auth=None
      )
      return defaultdict(lambda: local_repo)
    else:
      return self.repos

  def coordinate(self, org, name, rev=None):
    return '{}#{};{}'.format(org, name, rev) if rev else '{}#{}'.format(org, name)

  def jar_coordinate(self, jar, rev=None):
    return self.coordinate(jar.org, jar.name, rev or jar.rev)

  def generate_pom(self, tgt, version, path):
    closure = tgt.closure()
    pom_deps = [t for t in closure if isinstance(t, PomTarget) and t is not tgt]
    for pom in pom_deps:
      closure -= pom.closure()
    dependencies = OrderedDict()
    for dep in closure:
      if isinstance(dep, PomTarget):
        dep_jar = TemplateData(
          artifact_id=dep.payload.provides.name,
          group_id=dep.payload.provides.org,
          version=version,
          scope='compile',
        )
        key = (dep.payload.provides.org, dep.payload.provides.name)
        dependencies[key] = dep_jar
      elif isinstance(dep, Resources):
        pass
      elif isinstance(dep, JarLibrary):
        for jar in dep.jar_dependencies:
          dep_jar = TemplateData(
            artifact_id=jar.name,
            group_id=jar.org,
            version=jar.rev,
            scope='compile',
          )
          key = (jar.org, jar.name, jar.classifier)
          dependencies[key] = dep_jar
      else:
        pass

    # TODO(mateo): This needs to be configurable - preferably as a dependency or at least an option.
    # We are now using it for internal libs - so this confusing and should be fixed soon-ish.
    target_jar = TemplateData(
      artifact_id=tgt.payload.provides.name,
      group_id=tgt.payload.provides.org,
      version=version,
      scope='compile',
      dependencies=dependencies.values(),
      # TODO(dan): These should really come from an OSSRHPublicationMetadata
      #   instance, but it might have to be made a Target first so we don't
      #   duplicate it for every PomTarget.
      name='fsq.io',
      description='Foursquare Opensource',
      url='http://github.com/foursquare/fsqio',
      licenses=[TemplateData(
        name='Apache',
        url='http://www.opensource.org/licenses/Apache-2.0',
      )],
      scm=TemplateData(
        url='git@github.com:foursquare/spindle.git',
        # TODO(dan): Are these the right values?
        connection='scm:git:git@github.com:foursquare/fsqio.git',
        developer_connection='scm:git:git@github.com:foursquare/fsqio.git',
      ),
      developers=[
        TemplateData(
          id='paperstreet',
          name='Daniel Harrison',
          url='https://github.com/paperstreet',
        ),
        TemplateData(
          id='mateor',
          name='Mateo Rodriguez',
          url='https://github.com/mateor',
        ),
      ],
    )

    template_relpath = os.path.join(_TEMPLATES_RELPATH, 'pom.mustache')
    template_text = pkgutil.get_data(__name__, template_relpath)
    generator = Generator(template_text, project=target_jar)
    with safe_open(path, 'wb') as output:
      generator.write(output)

  def create_doc_jar(self, target, open_jar, version):
    # TODO(dan): make this ship javadoc. All the javadoc is broken.
    return None

  def create_source_jar(self, target, open_jar, version):

    def abs_and_relative_sources(target):
      abs_source_root = os.path.join(get_buildroot(), target.target_base)
      for source in target.sources_relative_to_source_root():
        yield os.path.join(abs_source_root, source), source

    jar_path = self.artifact_path(open_jar, version, suffix='-sources')
    with self.open_jar(jar_path, overwrite=True, compressed=True) as open_jar:
      for abs_source, rel_source in abs_and_relative_sources(target):
        open_jar.write(abs_source, rel_source)

      # TODO(Tejal Desai): pantsbuild/pants/65 Remove java_sources attribute for ScalaLibrary
      for dep in target.dependencies:
        if isinstance(dep, ScalaLibrary):
          for java_source_target in dep.java_sources:
            for abs_source, rel_source in abs_and_relative_sources(java_source_target):
              open_jar.write(abs_source, rel_source)

        if dep.has_resources:
          for resource_target in dep.resources:
            for abs_source, rel_source in abs_and_relative_sources(resource_target):
              open_jar.write(abs_source, rel_source)

    return jar_path

  def is_jarable_target(self, tgt):
    return isinstance(tgt, Jarable) or isinstance(tgt, Resources)

  def check_target(self, tgt):
    transitive_deps = tgt.closure()
    transitive_deps.remove(tgt)

    transitive_pom_deps = set(t for t in transitive_deps if isinstance(t, PomTarget))

    def gather_jarables(deps):
      jarables = set()
      for dep in deps:
        if self.is_jarable_target(dep):
          if dep in jarables:
            raise TaskError('A jarable dependency is listed twice in the PomPublish closure: {}\n'
              '    * {}'.format(tgt.address.spec, dep))
          jarables.add(dep)
      return jarables

    required_jarable_deps = gather_jarables(transitive_deps)

    accounted_jarable_deps = set(t for t in tgt.dependencies if self.is_jarable_target(t))
    for p in transitive_pom_deps:
      accounted_jarable_deps.update(t for t in p.dependencies if self.is_jarable_target(t))

    missing_jarable_deps = required_jarable_deps - accounted_jarable_deps
    if len(missing_jarable_deps):
      missing_deps = '\n  '.join([d.address.spec for d in sorted(list(missing_jarable_deps))])
      raise TaskError('Missing jarable deps in {}:\n  {}'.format(tgt.address.spec, missing_deps))

  def stage_artifacts(self, tgt, jar, version):
    publications = OrderedSet()

    jar_path = self.artifact_path(jar, version, extension='jar')
    safe_mkdir(os.path.dirname(jar_path))

    with self.context.new_workunit(name='create-monolithic-jar'):
      with self.open_jar(jar_path, overwrite=True, compressed=True) as monolithic_jar:
        with self.context.new_workunit(name='add-internal-classes'):
          with self.create_jar_builder(monolithic_jar) as jar_builder:
            for dep in tgt.dependencies:
              if self.is_jarable_target(dep):
                jar_builder.add_target(dep, recursive=False)

        publications.add(self.Publication(name=tgt.provides.name, classifier=None, ext='jar'))

    # NOTE(mateo): I tried turning on source_jars but it is bugged and only made source jars for a few modules.
    # sources_jar_path = self.create_source_jar(tgt, jar, version)
    # TODO(dan): Actually add source jar.
    source_jar_path = self.artifact_path(jar, version, suffix='-sources', extension='jar')
    with self.open_jar(source_jar_path, overwrite=True, compressed=True) as source_jar:
      source_jar.writestr('EMPTY', 'EMPTY'.encode('utf-8'))
    publications.add(self.Publication(name=jar.name, classifier='sources', ext='jar'))

    # TODO(dan): Actually add doc jar.
    doc_jar_path = self.artifact_path(jar, version, suffix='-javadoc', extension='jar')
    with self.open_jar(doc_jar_path, overwrite=True, compressed=True) as doc_jar:
      doc_jar.writestr('EMPTY', 'EMPTY'.encode('utf-8'))
    publications.add(self.Publication(name=jar.name, classifier='javadoc', ext='jar'))

    pom_path = self.artifact_path(jar, version, extension='pom')
    self.generate_pom(tgt, version=version, path=pom_path)
    return publications

  def execute(self):
    safe_rmtree(self.workdir)
    published = []
    pom_targets = [t for t in self.context.targets() if isinstance(t, PomTarget)]
    for tgt in pom_targets:
      self.check_target(tgt)

    for tgt in pom_targets:
      try:
        repo = self.get_repos[tgt.provides.repo.name]
      except KeyError:
        raise TaskError('Repository {0} has no entry in the --repos option.'.format(tgt.provides.repo.name))
      jar = JarDependency(org=tgt.provides.org, name=tgt.provides.name, rev=None)
      published.append(jar)

      publications = self.stage_artifacts(tgt, jar, self.version)
      self.publish(publications, jar=jar, repo=repo, version=self.version, published=published)

  def publish(self, publications, jar, version, repo, published):
    """Run ivy to publish a jar.

    :param str ivyxml_path: The path to the ivy file.
    :param list published: A list of jars published so far (including this one).
    """

    try:
      ivy = Bootstrapper.default_ivy()
    except Bootstrapper.Error as e:
      raise TaskError('Failed to push {0}! {1}'.format(jar, e))

    if ivy.ivy_settings is None:
      raise TaskError('An ivysettings.xml with writeable resolvers is required for publishing, '
                      'but none was configured.')

    path = repo.get('path')

    ivysettings = self.generate_ivysettings(self.fetch_ivysettings(ivy), published, publish_local=path)
    ivyxml = self.generate_ivy(jar, version, publications)
    resolver = repo['resolver']
    args = [
      '-settings', ivysettings,
      '-ivy', ivyxml,

      # Without this setting, the ivy.xml is delivered to the CWD, littering the workspace.  We
      # don't need the ivy.xml, so just give it path under the workdir we won't use.
      '-deliverto', ivyxml + '.unused',

      '-publish', resolver,
      '-publishpattern', '{}/[organisation]/[module]/'
                         '[artifact]-[revision](-[classifier]).[ext]'.format(self.workdir),
      '-revision', version,
      '-m2compatible',
    ]

    if self.get_options().level == 'debug':
      args.append('-verbose')

    if self.get_options().local:
      args.append('-overwrite')

    if not self.get_options().dryrun:
      try:
        ivy.execute(jvm_options=self._ivy_jvm_options(repo), args=args,
                    workunit_factory=self.context.new_workunit, workunit_name='ivy-publish')
      except Ivy.Error as e:
        raise TaskError('Failed to push {0}! {1}'.format(jar, e))
    else:
      print("\nDRYRUN- would have pushed:{} using {} resolver.".format(self.jar_coordinate(jar, version), resolver))

