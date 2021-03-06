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

"""Module used to define functions and classes used by the coder unit tests."""

import re


class TopClass(object):

  class NestedClass(object):

    def __init__(self, datum):
      self.datum = 'X:%s' % datum

  class MiddleClass(object):

    class NestedClass(object):

      def __init__(self, datum):
        self.datum = 'Y:%s' % datum


def get_lambda_with_globals():
  return lambda s: re.findall(r'\w+', s)


def get_lambda_with_closure(message):
  return lambda: 'closure: %s' % message


class Xyz(object):
  """A class to be pickled."""

  def foo(self, s):
    return re.findall(r'\w+', s)


XYZ_OBJECT = Xyz()
