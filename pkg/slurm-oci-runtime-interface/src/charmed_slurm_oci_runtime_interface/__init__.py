# Copyright 2025-2026 Canonical Ltd.
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

"""Integration interface implementation for the `slurm_oci_runtime` interface."""

__all__ = [
    "OCIRuntimeData",
    "OCIRuntimeDisconnectedEvent",
    "OCIRuntimeReadyEvent",
    "OCIRuntimeProvider",
    "OCIRuntimeRequirer",
]

from typing import Any, Literal

import ops
from charmed_hpc_libs.ops import leader
from charmed_slurm_slurmctld_interface import SlurmctldProvider, SlurmctldRequirer
from pydantic.dataclasses import dataclass


@dataclass(frozen=True)
class OCIRuntimeData:
    """Data provided by the OCI runtime provider.

    Attributes:
        type: The type of Slurm-compatible OCI runtime.
        executable_path: Path to the OCI runtime executable.
    """

    type: Literal["apptainer"]
    executable_path: str


class OCIRuntimeReadyEvent(ops.RelationEvent):
    """Event emitted when the OCI runtime application leader is ready.

    Notes:
        - The OCI runtime application leader is "ready" once it is installed on
          each principal unit and able to share its configuration information
          required by the Slurm controller `slurmctld`.
    """


class OCIRuntimeDisconnectedEvent(ops.RelationEvent):
    """Event emitted when the OCI runtime application is disconnected from `slurmctld`."""


class _OCIRunTimeRequirerEvents(ops.ObjectEvents):
    """`slurm_oci_runtime` requirer events."""

    oci_runtime_ready = ops.EventSource(OCIRuntimeReadyEvent)
    oci_runtime_disconnected = ops.EventSource(OCIRuntimeDisconnectedEvent)


class OCIRuntimeProvider(SlurmctldRequirer):
    """Integration interface implementation for `slurm_oci_runtime` providers.

    Notes:
        - This interface should be used on the OCI runtime application leader to
          provide OCI runtime information to the `slurmctld` application leader.
        - Only the leading `oci_runtime` unit should interact with `slurmctld`.
          All other `oci_runtime` units are peers to be directed by the leader.
    """

    @leader
    def _on_relation_created(self, event: ops.RelationCreatedEvent) -> None:
        super()._on_relation_created(event)

    @leader
    def _on_relation_changed(self, event: ops.RelationChangedEvent) -> None:
        super()._on_relation_changed(event)

    @leader
    def _on_relation_broken(self, event: ops.RelationBrokenEvent) -> None:
        if self._stored.unit_departing:
            return

        super()._on_relation_broken(event)

    @leader
    def set_oci_runtime_data(  # noqa D417
        self,
        data: OCIRuntimeData,
        /,
        integration_id: int | None = None,
        **kwargs: Any,
    ) -> None:
        """Set OCI runtime data in the `slurm_oci_runtime` application databag.

        Args:
            data: OCI runtime data to set on an integrations' application databag.
            integration_id:
                ID of integration to update. If no integration ID is passed,
                all integrations will be updated.

        Keyword Args:
            merge:
                Whether to merge ``data`` into the integration databag rather than
                overwriting. When ``True``, only fields whose values differ from their
                dataclass defaults are written; existing values for unset fields are
                preserved. Defaults to ``False``.
            reset:
                Set of dataclass fields to reset to their default value when
                ``merge`` is ``True``. Has precedence over `data`. Defaults to an
                empty set.

        Warnings:
            - Only the OCI runtime application leader can set OCI runtime configuration data.
        """
        self._save_integration_data(data, self.app, integration_id, **kwargs)


class OCIRuntimeRequirer(SlurmctldProvider):
    """Integration interface implementation for `slurm_oci_runtime` requirers.

    Notes:
        - This interface should be used on the `slurmctld` application leader
          to retrieve data from the OCI runtime provider and edit the `oci.conf`
          configuration file.
    """

    on = _OCIRunTimeRequirerEvents()  # type: ignore

    def __init__(self, charm: ops.CharmBase, integration_name: str) -> None:
        super().__init__(charm, integration_name)

        self.framework.observe(
            self.charm.on[self._integration_name].relation_changed,
            self._on_relation_changed,
        )
        self.framework.observe(
            self.charm.on[self._integration_name].relation_broken,
            self._on_relation_broken,
        )

    @leader
    def _on_relation_changed(self, event: ops.RelationChangedEvent) -> None:
        """Handle when data from the OCI runtime application leader is ready."""
        if not event.relation.data.get(event.relation.app):
            return

        self.on.oci_runtime_ready.emit(event.relation)

    @leader
    def _on_relation_broken(self, event: ops.RelationBrokenEvent) -> None:
        if self._stored.unit_departing:
            return

        self.on.oci_runtime_disconnected.emit(event.relation)

    def get_oci_runtime_data(self, integration_id: int | None = None) -> OCIRuntimeData:
        """Get OCI runtime data from the `slurm_oci_runtime` application databag.

        Args:
            integration_id: ID of integration to pull OCI runtime configuration data from.
        """
        return self._load_integration_data(OCIRuntimeData, integration_id=integration_id).pop()
