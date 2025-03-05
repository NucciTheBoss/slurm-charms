#!/usr/bin/env python3
# Copyright 2020-2025 Omnivector, LLC.
# See LICENSE file for licensing details.

"""Slurmd Operator Charm."""

import logging
from itertools import chain
from pathlib import Path
from typing import Any, cast

import ops
from constants import SLURMD_INTEGRATION_NAME
from machine import GPUOpsError, RDMAOpsError, gpu, nhc, rdma, service
from ops import (
    ActiveStatus,
    BlockedStatus,
    ConfigChangedEvent,
    MaintenanceStatus,
    StoredState,
    UpdateStatusEvent,
)
from slurmutils.models import GRESNode, GRESNodeMapping, Node, Partition
from slurmutils.models.option import PartitionOptionSet
from utils import controller_not_ready, refresh

from charms.hpc_libs.v0.slurm_interfaces import (
    SlurmctldConnectedEvent,
    SlurmctldReadyEvent,
    SlurmctldUnavailableEvent,
    SlurmdProvider,
)
from charms.hpc_libs.v0.slurm_ops import SlurmdManager, SlurmOpsError
from charms.hpc_libs.v0.macros import StopCharm, wait_when
from charms.operator_libs_linux.v0.juju_systemd_notices import (  # type: ignore[import-untyped]
    ServiceStartedEvent,
    ServiceStoppedEvent,
    SystemdNotices,
)

logger = logging.getLogger(__name__)


class SlurmdCharm(ops.CharmBase):
    """Charmed operator for slurmd, the compute node daemon of Slurm."""

    _stored = StoredState()

    def __init__(self, framework: ops.Framework) -> None:
        super().__init__(framework)

        self._stored.set_default(
            munge_key=str(),
            new_node=True,
            nhc_conf=str(),
            nhc_params=str(),
            slurm_installed=False,
            slurmctld_available=False,
            slurmctld_host=str(),
            user_supplied_node_parameters={},
            user_supplied_partition_parameters={},
        )

        self._slurmd = SlurmdManager(snap=False)
        self._slurmctld = SlurmdProvider(self, SLURMD_INTEGRATION_NAME)
        self._systemd_notices = SystemdNotices(self, ["slurmd"])

        framework.observe(self.on.install, self._on_install)
        framework.observe(self.on.config_changed, self._on_config_changed)
        framework.observe(self.on.update_status, self._on_update_status)
        framework.observe(self._slurmctld.on.slurmctld_connected, self._on_slurmctld_connected)
        framework.observe(self._slurmctld.on.slurmctld_ready, self._on_slurmctld_ready)
        framework.observe(self._slurmctld.on.slurmctld_unavailable, self._on_slurmctld_unavailable)
        framework.observe(self.on.service_slurmd_started, self._on_slurmd_started)
        framework.observe(self.on.service_slurmd_stopped, self._on_slurmd_stopped)

    @refresh
    def _on_install(self, event: ops.InstallEvent) -> None:
        """Perform installation operations for slurmd."""
        # Account for case where base image has been auto-upgraded by Juju and a reboot is pending
        # before charm code runs. Reboot "now", before the current hook completes, and restart the
        # hook after reboot. Prevents issues such as drivers/kernel modules being installed for a
        # running kernel pending replacement by a newer version on reboot.
        self._reboot_if_required(now=True)

        self.unit.status = MaintenanceStatus("Installing slurmd")

        try:
            self._slurmd.install()

            self.unit.status = MaintenanceStatus("Installing nhc")
            nhc.install()

            self.unit.status = MaintenanceStatus("Installing RDMA packages")
            rdma.install()

            self.unit.status = MaintenanceStatus("Detecting if machine is GPU-equipped")
            gpu_enabled = gpu.autoinstall()
            if gpu_enabled:
                self.unit.status = MaintenanceStatus("Successfully installed GPU drivers")
            else:
                self.unit.status = MaintenanceStatus("No GPUs found. Continuing")

            # TODO: https://github.com/orgs/charmed-hpc/discussions/10 -
            #   Evaluate if we should continue doing the service override here
            #   for `juju-systemd-notices`.
            service.override_service()
            self._systemd_notices.subscribe()

            self.unit.set_workload_version(self._slurmd.version())
            self._stored.slurm_installed = True
        except (GPUOpsError, RDMAOpsError, SlurmOpsError) as e:
            logger.error(e.message)
            event.defer()
            raise StopCharm(ops.BlockedStatus("Install failed. See `juju debug-log` for details"))

        self._reboot_if_required()

    # TODO: This is a fucking mess. Clean it up.
    def _on_config_changed(self, _: ConfigChangedEvent) -> None:
        """Handle charm configuration changes."""
        # Casting the type to str is required here because `get` returns a looser
        # type than what `nhc.generate_config(...)` allows to be passed.
        if nhc_conf := cast(str, self.model.config.get("nhc-conf", "")):
            if nhc_conf != self._stored.nhc_conf:
                self._stored.nhc_conf = nhc_conf
                nhc.generate_config(nhc_conf)

        user_supplied_partition_parameters = self.model.config.get("partition-config")

        if self.model.unit.is_leader():
            if user_supplied_partition_parameters is not None:
                try:
                    tmp_params = {
                        item.split("=")[0]: item.split("=")[1]
                        for item in str(user_supplied_partition_parameters).split()
                    }
                except IndexError:
                    logger.error(
                        "Error parsing partition-config. Please use KEY1=VALUE KEY2=VALUE."
                    )
                    return

                # Validate the user supplied params are valid params.
                for parameter in tmp_params:
                    if parameter not in list(PartitionOptionSet.keys()):
                        logger.error(
                            f"Invalid user supplied partition configuration parameter: {parameter}."
                        )
                        return

                self._stored.user_supplied_partition_parameters = tmp_params

                # if self._slurmctld.is_joined:
                #     self._slurmctld.set_partition()

    @refresh
    def _on_update_status(self, _: UpdateStatusEvent) -> None:
        """Handle update status."""

    @refresh
    def _on_slurmctld_connected(self, event: SlurmctldConnectedEvent) -> None:
        """Handle when controller is connected to compute node."""
        if self.unit.is_leader():
            partition = Partition(
                PartitionName=self.app.name,
                State="UP",
                **self._stored.user_supplied_partition_parameters,
            )
            self._slurmctld.set_partition_data(partition, event.relation.id)

        node = cast(Node, self._slurmd.get_hardware_info())
        node.node_name = self.unit.name
        node.node_hostname = self._slurmd.hostname
        node.mem_spec_limit = "1024"
        node.update(self._user_supplied_node_parameters)

        gres = GRESNodeMapping()
        for res in chain.from_iterable(self._slurmd.get_gres_info().values()):
            spec = f"{res.name}:{res.type}:{res.count}"
            if node.gres:
                node.gres.append(spec)
            else:
                node.gres = [spec]

            gres[node.node_name] = gres.get(node.node_name) + [
                GRESNode(NodeName=node.node_name, Name=res.name, Type=res.type, File=res.file)
            ]

        self._slurmctld.set_node_data(node=node, gres=gres, integration_id=event.relation.id)

    @refresh
    @wait_when(controller_not_ready)
    def _on_slurmctld_ready(self, event: SlurmctldReadyEvent) -> None:
        """Handle when controller provides hostnames and authentication key."""
        data = self._slurmctld.get_controller_data(integration_id=event.relation.id)
        self._slurmd.config_server = ",".join(f"{host}:6817" for host in data.hostnames)
        self._slurmd.munge.key.set(data.auth_key)

        try:
            self._slurmd.munge.service.restart()
            self._slurmd.service.restart()
            self._slurmd.service.enable()
        except SlurmOpsError as e:
            logger.error(e.message)

    @refresh
    def _on_slurmctld_unavailable(self, _: SlurmctldUnavailableEvent) -> None:
        """Handle when compute node is disconnected from controller."""
        logger.info("stopping and disabling `slurmd` service")
        self._slurmd.service.stop()
        self._slurmd.service.disable()
        del self._slurmd.config_server

    def _on_slurmd_started(self, _: ServiceStartedEvent) -> None:
        """Handle event emitted by systemd after slurmd daemon successfully starts."""
        self.unit.status = ActiveStatus()

    def _on_slurmd_stopped(self, _: ServiceStoppedEvent) -> None:
        """Handle event emitted by systemd after slurmd daemon is stopped."""
        self.unit.status = BlockedStatus("slurmd not running")

    # def _on_node_configured_action(self, _: ActionEvent) -> None:
    #     """Remove node from DownNodes and mark as active."""
    #     # Trigger reconfiguration of slurmd node.
    #     # TODO: We need to get rid of this. No point triggering a cluster wide event because of a
    #     #   single change against one machine.
    #     self._new_node = False
    #     self._slurmctld.set_node()
    #     self._slurmd.service.restart()
    #     logger.debug("### This node is not new anymore")
    #
    # def _on_show_nhc_config(self, event: ActionEvent) -> None:
    #     """Show current nhc.conf."""
    #     try:
    #         event.set_results({"nhc.conf": nhc.get_config()})
    #     except FileNotFoundError:
    #         event.set_results({"nhc.conf": "/etc/nhc/nhc.conf not found."})
    #
    # def _on_node_config_action_event(self, event: ActionEvent) -> None:
    #     """Get or set the user_supplied_node_config.
    #
    #     Return the node config if the `node-config` parameter is not specified, otherwise
    #     parse, validate, and store the input of the `node-config` parameter in stored state.
    #     Lastly, update slurmctld if there are updates to the node config.
    #     """
    #     valid_config = True
    #     config_supplied = False
    #
    #     if (user_supplied_node_parameters := event.params.get("parameters")) is not None:
    #         config_supplied = True
    #
    #         # Parse the user supplied node-config.
    #         node_parameters_tmp = {}
    #         try:
    #             node_parameters_tmp = {
    #                 item.split("=")[0]: item.split("=")[1]
    #                 for item in user_supplied_node_parameters.split()
    #             }
    #         except IndexError:
    #             logger.error(
    #                 "Invalid node parameters specified. Please use KEY1=VAL KEY2=VAL format."
    #             )
    #             valid_config = False
    #
    #         # Validate the user supplied params are valid params.
    #         for param in node_parameters_tmp:
    #             if param not in list(NodeOptionSet.keys()):
    #                 logger.error(f"Invalid user supplied node parameter: {param}.")
    #                 valid_config = False
    #
    #         # Validate the user supplied params have valid keys.
    #         for k, v in node_parameters_tmp.items():
    #             if v == "":
    #                 logger.error(f"Invalid user supplied node parameter: {k}={v}.")
    #                 valid_config = False
    #
    #         if valid_config:
    #             if (node_parameters := node_parameters_tmp) != self._user_supplied_node_parameters:
    #                 self._user_supplied_node_parameters = node_parameters
    #                 self._slurmctld.set_node()
    #
    #     results = {
    #         "node-parameters": " ".join(
    #             [f"{k}={v}" for k, v in self.get_node()["node_parameters"].items()]
    #         )
    #     }
    #
    #     if config_supplied is True:
    #         results["user-supplied-node-parameters-accepted"] = f"{valid_config}"
    #
    #     event.set_results(results)
    #
    # @property
    # def hostname(self) -> str:
    #     """Return the hostname."""
    #     return self._slurmd.hostname
    #
    @property
    def _user_supplied_node_parameters(self) -> dict[Any, Any]:
        """Return the user_supplied_node_parameters from stored state."""
        return self._stored.user_supplied_node_parameters  # type: ignore[return-value]

    @_user_supplied_node_parameters.setter
    def _user_supplied_node_parameters(self, node_parameters: dict) -> None:
        """Set the node_parameters in stored state."""
        self._stored.user_supplied_node_parameters = node_parameters

    #
    # @property
    # def _new_node(self) -> bool:
    #     """Get the new_node from stored state."""
    #     return True if self._stored.new_node is True else False
    #
    # @_new_node.setter
    # def _new_node(self, new_node: bool) -> None:
    #     """Set the new_node in stored state."""
    #     self._stored.new_node = new_node
    #
    # # TODO: We could probably nuke this, on god.
    # def _check_status(self) -> bool:
    #     """Check if we have all needed components.
    #
    #     - slurmd installed
    #     - slurmctld available and working
    #     - munge key configured and working
    #     """
    #     if self._stored.slurm_installed is not True:
    #         self.unit.status = BlockedStatus("Install failed. See `juju debug-log` for details")
    #         return False
    #
    #     if self._slurmctld.is_joined is not True:
    #         self.unit.status = BlockedStatus("Need relations: slurmctld")
    #         return False
    #
    #     if self._stored.slurmctld_available is not True:
    #         self.unit.status = WaitingStatus("Waiting on: slurmctld")
    #         return False
    #
    #     # TODO: https://github.com/charmed-hpc/hpc-libs/issues/18 -
    #     #   Re-enable munge key validation check check when supported by `slurm_ops` charm library.
    #     # if not self._slurmd.check_munged():
    #     #     self.unit.status = BlockedStatus("Error configuring munge key")
    #     #     return False
    #
    #     return True

    def _reboot_if_required(self, now: bool = False) -> None:
        """Perform a reboot of the unit if required, e.g. following a driver installation."""
        if Path("/var/run/reboot-required").exists():
            logger.info("rebooting unit %s", self.unit.name)
            self.unit.reboot(now)


if __name__ == "__main__":  # pragma: nocover
    ops.main(SlurmdCharm)
