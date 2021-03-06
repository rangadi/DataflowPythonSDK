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

cimport libc.stdint


cdef class OutputStream(object):
  cdef char* data
  cdef size_t size
  cdef size_t pos

  cpdef write(self, bytes b, bint nested=*)
  cpdef write_byte(self, unsigned char val)
  cpdef write_var_int64(self, libc.stdint.int64_t v)

  cpdef bytes get(self)

  cdef extend(self, size_t missing)


cdef class InputStream(object):
  cdef size_t pos
  cdef bytes all
  cdef char* allc

  cpdef size_t size(self) except? -1
  cpdef bytes read(self, size_t len)
  cpdef long read_byte(self) except? -1
  cpdef libc.stdint.int64_t read_var_int64(self) except? -1
  cpdef bytes read_all(self, bint nested=*)
