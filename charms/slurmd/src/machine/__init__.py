# Copyright 2023-2025 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Machine-specific modules used within the slurmd charm.

Modules:
    - `gpu`: Manage GPU-related operations.
    - `nhc`: Manage Node Health Check (NHC) operations.
    - `rdma`: Manage RDMA operations.
    - `service`: Manage custom systemd service overrides for slurmd.
"""

from . import gpu as gpu
from . import nhc as nhc
from . import rdma as rdma
from . import service as service
from .gpu import GPUOpsError as GPUOpsError
from .rdma import RDMAOpsError as RDMAOpsError
