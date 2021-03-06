# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Support for installing custom code and required dependencies.

Workflows, with the exception of very simple ones, are organized in multiple
modules and packages. Typically, these modules and packages have
dependencies on other standard libraries. Dataflow relies on the Python
setuptools package to handle these scenarios. For further details please read:
https://pythonhosted.org/setuptools/setuptools.html

When a runner tries to run a pipeline it will check for a --requirements_file
and a --setup_file option.

If --setup_file is present then it is assumed that the folder containing the
file specified by the option has the typical layout required by setuptools and
it will run 'python setup.py sdist' to produce a source distribution. The
resulting tarball (a file ending in .tar.gz) will be staged at the GCS staging
location specified as job option. When a worker starts it will check for the
presence of this file and will run 'easy_install tarball' to install the
package in the worker.

If --requirements_file is present then the file specified by the option will be
staged in the GCS staging location.  When a worker starts it will check for the
presence of this file and will run 'pip install -r requirements.txt'. A
requirements file can be easily generated by running 'pip freeze -r
requirements.txt'. The reason a Dataflow runner does not run this automatically
is because quite often only a small fraction of the dependencies present in a
requirements.txt file are actually needed for remote execution and therefore a
one-time manual trimming is desirable.

TODO(silviuc): Staged files should have a job specific prefix.
To prevent several jobs in the same project stomping on each other due to a
shared staging location.

TODO(silviuc): Should we allow several setup packages?
TODO(silviuc): We should allow customizing the exact command for setup build.
"""

import glob
import logging
import os
import shutil
import tempfile


from google.cloud.dataflow import utils
from google.cloud.dataflow.internal import pickler
from google.cloud.dataflow.utils import names
from google.cloud.dataflow.utils import processes
from google.cloud.dataflow.utils.options import GoogleCloudOptions
from google.cloud.dataflow.utils.options import SetupOptions
from google.cloud.dataflow.version import __version__


# Standard file names used for staging files.
WORKFLOW_TARBALL_FILE = 'workflow.tar.gz'
REQUIREMENTS_FILE = 'requirements.txt'
EXTRA_PACKAGES_FILE = 'extra_packages.txt'


def _dependency_file_copy(from_path, to_path):
  """Copies a local file to a GCS file or vice versa."""
  logging.info('file copy from %s to %s.', from_path, to_path)
  if from_path.startswith('gs://') or to_path.startswith('gs://'):
    command_args = ['gsutil', '-m', '-q', 'cp', from_path, to_path]
    logging.info('Executing command: %s', command_args)
    result = processes.call(command_args)
    if result != 0:
      raise ValueError(
          'Failed to copy GCS file from %s to %s.' % (from_path, to_path))
  else:
    # Branch used only for unit tests and integration tests.
    # In such environments GCS support is not available.
    if not os.path.isdir(os.path.dirname(to_path)):
      logging.info('Created folder (since we have not done yet, and any errors '
                   'will follow): %s ', os.path.dirname(to_path))
      os.mkdir(os.path.dirname(to_path))
    shutil.copyfile(from_path, to_path)


def _stage_extra_packages(extra_packages,
                          staging_location,
                          file_copy=_dependency_file_copy, temp_dir=None):
  """Stages a list of local extra packages.

  Args:
    extra_packages: Ordered list of local paths to extra packages to be staged.
    staging_location: Staging location for the packages.
    file_copy: Callable for copying files. The default version will copy from
      a local file to a GCS location using the gsutil tool available in the
      Google Cloud SDK package.
    temp_dir: Temporary folder where the resource building can happen. If None
      then a unique temp directory will be created. Used only for testing.

  Returns:
    A list of file names (no paths) for the resources staged. All the files
    are assumed to be staged in staging_location.

  Raises:
    RuntimeError: If files specified are not found or do not have expected
      name patterns.
  """
  resources = []
  for package in extra_packages:
    if not os.path.isfile(package):
      raise RuntimeError(
          'The file %s cannot be found. It was specified in the '
          '--extra_packages command line option.' % package)
    if not os.path.basename(package).endswith('.tar.gz'):
      raise RuntimeError(
          'The --extra_packages option expects a full path ending with '
          '\'.tar.gz\' instead of %s' % package)
    basename = os.path.basename(package)
    staged_path = utils.path.join(staging_location, basename)
    file_copy(package, staged_path)
    resources.append(basename)
  # Create a file containing the list of extra packages and stage it.
  # The file is important so that in the worker the packages are installed
  # exactly in the order specified. This approach will avoid extra PyPI
  # requests. For example if package A depends on package B and package A
  # is installed first then the installer will try to satisfy the
  # dependency on B by downloading the package from PyPI. If package B is
  # installed first this is avoided.
  with open(os.path.join(temp_dir, EXTRA_PACKAGES_FILE), 'wt') as f:
    for package in extra_packages:
      f.write('%s\n' % os.path.basename(package))
  staged_path = utils.path.join(staging_location, EXTRA_PACKAGES_FILE)
  # Note that the caller of this function is responsible for deleting the
  # temporary folder where all temp files are created, including this one.
  file_copy(os.path.join(temp_dir, EXTRA_PACKAGES_FILE), staged_path)
  resources.append(EXTRA_PACKAGES_FILE)
  return resources


def stage_job_resources(options, file_copy=_dependency_file_copy,
                        build_setup_args=None, temp_dir=None):
  """Creates (if needed) and stages job resources to options.staging_location.

  Args:
    options: Command line options. More specifically the function will expect
      staging_location, requirements_file, setup_file, and save_main_session
      options to be present.
    file_copy: Callable for copying files. The default version will copy from
      a local file to a GCS location using the gsutil tool available in the
      Google Cloud SDK package.
    build_setup_args: A list of command line arguments used to build a setup
      package. Used only if options.setup_file is not None. Used only for
      testing.
    temp_dir: Temporary folder where the resource building can happen. If None
      then a unique temp directory will be created. Used only for testing.

  Returns:
    A list of file names (no paths) for the resources staged. All the files
    are assumed to be staged in options.staging_location.

  Raises:
    RuntimeError: If files specified are not found or error encountered while
      trying to create the resources (e.g., build a setup package).
  """
  temp_dir = temp_dir or tempfile.mkdtemp()
  resources = []

  google_cloud_options = options.view_as(GoogleCloudOptions)
  setup_options = options.view_as(SetupOptions)
  # Make sure that all required options are specified. There are a few that have
  # defaults to support local running scenarios.
  if google_cloud_options.staging_location is None:
    raise RuntimeError(
        'The --staging_location option must be specified.')
  if google_cloud_options.temp_location is None:
    raise RuntimeError(
        'The --temp_location option must be specified.')

  # Stage a requirements file if present.
  if setup_options.requirements_file is not None:
    if not os.path.isfile(setup_options.requirements_file):
      raise RuntimeError('The file %s cannot be found. It was specified in the '
                         '--requirements_file command line option.' %
                         setup_options.requirements_file)
    staged_path = utils.path.join(google_cloud_options.staging_location,
                                  REQUIREMENTS_FILE)
    file_copy(setup_options.requirements_file, staged_path)
    resources.append(REQUIREMENTS_FILE)

  # Handle a setup file if present.
  # We will build the setup package locally and then copy it to the staging
  # location because the staging location is a GCS path and the file cannot be
  # created directly there.
  if setup_options.setup_file is not None:
    if not os.path.isfile(setup_options.setup_file):
      raise RuntimeError('The file %s cannot be found. It was specified in the '
                         '--setup_file command line option.' %
                         setup_options.setup_file)
    if os.path.basename(setup_options.setup_file) != 'setup.py':
      raise RuntimeError(
          'The --setup_file option expects the full path to a file named '
          'setup.py instead of %s' % setup_options.setup_file)
    tarball_file = _build_setup_package(setup_options.setup_file, temp_dir,
                                        build_setup_args)
    staged_path = utils.path.join(google_cloud_options.staging_location,
                                  WORKFLOW_TARBALL_FILE)
    file_copy(tarball_file, staged_path)
    resources.append(WORKFLOW_TARBALL_FILE)

  # Handle extra local packages that should be staged.
  if setup_options.extra_packages is not None:
    resources.extend(
        _stage_extra_packages(setup_options.extra_packages,
                              google_cloud_options.staging_location,
                              file_copy=file_copy,
                              temp_dir=temp_dir))

  # Pickle the main session if requested.
  # We will create the pickled main session locally and then copy it to the
  # staging location because the staging location is a GCS path and the file
  # cannot be created directly there.
  if setup_options.save_main_session:
    pickled_session_file = os.path.join(temp_dir,
                                        names.PICKLED_MAIN_SESSION_FILE)
    pickler.dump_session(pickled_session_file)
    staged_path = utils.path.join(google_cloud_options.staging_location,
                                  names.PICKLED_MAIN_SESSION_FILE)
    file_copy(pickled_session_file, staged_path)
    resources.append(names.PICKLED_MAIN_SESSION_FILE)

  if hasattr(setup_options, 'sdk_location') and setup_options.sdk_location:
    if setup_options.sdk_location == 'default':
      stage_tarball_from_gcs = True
    elif setup_options.sdk_location.startswith('gs://'):
      stage_tarball_from_gcs = True
    else:
      stage_tarball_from_gcs = False

    staged_path = utils.path.join(google_cloud_options.staging_location,
                                  names.DATAFLOW_SDK_TARBALL_FILE)
    if stage_tarball_from_gcs:
      # Unit tests running in the 'python setup.py test' context will
      # not have the sdk_location attribute present and therefore we
      # will not stage a tarball.
      if setup_options.sdk_location == 'default':
        sdk_folder = 'gs://dataflow-sdk-for-python'
      else:
        sdk_folder = setup_options.sdk_location
      _stage_dataflow_sdk_tarball(sdk_folder, staged_path)
      resources.append(names.DATAFLOW_SDK_TARBALL_FILE)
    else:
      # Check if we have a local Dataflow SDK tarball present. This branch is
      # used by tests running with the SDK built at head.
      if setup_options.sdk_location == 'default':
        module_path = os.path.abspath(__file__)
        sdk_dir = os.path.join(os.path.dirname(module_path), '..')
      else:
        sdk_dir = setup_options.sdk_location
      sdk_path = os.path.join(sdk_dir, names.DATAFLOW_SDK_TARBALL_FILE)
      if os.path.isfile(sdk_path):
        logging.info('Copying dataflow SDK "%s" to staging location.', sdk_path)
        file_copy(sdk_path, staged_path)
        resources.append(names.DATAFLOW_SDK_TARBALL_FILE)
      else:
        if setup_options.sdk_location == 'default':
          raise RuntimeError('Cannot find default Dataflow SDK tar file "%s"',
                             sdk_path)
        else:
          raise RuntimeError(
              'The file "%s" cannot be found. Its directory was specified by '
              'the --sdk_location command-line option.' %
              sdk_path)

  # Delete all temp files created while staging job resources.
  shutil.rmtree(temp_dir)
  return resources


def _build_setup_package(setup_file, temp_dir, build_setup_args=None):
  saved_current_directory = os.getcwd()
  try:
    os.chdir(os.path.dirname(setup_file))
    if build_setup_args is None:
      build_setup_args = [
          'python', os.path.basename(setup_file),
          'sdist', '--dist-dir', temp_dir]
    logging.info('Executing command: %s', build_setup_args)
    result = processes.call(build_setup_args)
    if result != 0:
      raise RuntimeError(
          'Failed to execute command: %s. Exit code %d',
          build_setup_args, result)
    output_files = glob.glob(os.path.join(temp_dir, '*.tar.gz'))
    if not output_files:
      raise RuntimeError(
          'File %s not found.' % os.path.join(temp_dir, '*.tar.gz'))
    return output_files[0]
  finally:
    os.chdir(saved_current_directory)


def _stage_dataflow_sdk_tarball(sdk_release_folder, staged_path):
  """Stage a Dataflow SDK tarball with the appropriate version.

  This function expects to find in the SDK release folder a file with the name
  google-cloud-dataflow-python-sdk-VERSION.tgz where VERSION is the version
  string for the containing SDK from google.cloud.dataflow.version.__version__.

  Args:
    sdk_release_folder: A local path or GCS path containing a Dataflow SDK
      tarball with the appropriate version.
    staged_path: GCS path where the found SDK tarball should be copied.
  """
  # Build the expected tarball file path.
  tarball_file_name = 'google-cloud-dataflow-python-sdk-%s.tgz' % __version__
  tarball_file_path = utils.path.join(sdk_release_folder, tarball_file_name)
  # Stage the file to the GCS staging area.
  logging.info(
      'Staging Dataflow SDK tarball from %s to %s',
      tarball_file_path, staged_path)
  _dependency_file_copy(tarball_file_path, staged_path)
