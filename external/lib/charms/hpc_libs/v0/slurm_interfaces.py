# Copyright 2025 Canonical Ltd.
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

"""Integration interfaces for the Slurm charms."""

__all__ = [
    "SackdProvider",
    "SackdRequirer",
    "SlurmctldProviderData",
    "SlurmdProvider",
    "SlurmdProviderData",
    "SlurmdRequirer",
    "SlurmdbdProvider",
    "SlurmdbdProviderData",
    "SlurmdbdRequirer",
    "SlurmrestdProvider",
    "SlurmrestdRequirer",
]

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from functools import wraps
from string import Template
from typing import Any, Self

import ops
from slurmutils.models import GRESConfig, GRESNodeMapping, Node, Partition, SlurmConfig
from slurmutils.models.model import BaseModel

LIBAPI = 0
LIBPATCH = 1
LIBID = "ABC123"
PYDEPS = ["slurmutils<1.0.0,>=0.11.0"]

AUTH_KEY_TEMPLATE_LABEL = Template("integration-$id-auth-key-secret")
JWT_KEY_TEMPLATE_LABEL = Template("integration-$id-jwt-key-secret")


def leader(func: Callable[..., Any]) -> Callable[..., Any]:
    """Only run method if the unit is the application leader, otherwise skip."""

    @wraps(func)
    def wrapper(interface, *args: Any, **kwargs: Any) -> Any:
        if not interface.unit.is_leader():
            return None

        return func(interface, *args, **kwargs)

    return wrapper


class _Secret:
    """Wrapper for interfacing with Slurm-related secrets."""

    def __init__(self, secret: ops.Secret = None) -> None:
        self._secret: ops.Secret = secret

    @property
    def uri(self) -> str:
        """Get the URI of this secret."""
        return self._secret.id if self._secret else ""

    @classmethod
    def create_or_update(cls, charm: ops.CharmBase, label: str, content: dict[str, str]) -> Self:
        """Create or update a secret.

        Args:
            charm: Charm to associate secret with.
            label: Secret label.
            content: Payload to set as secret content.
        """
        try:
            secret = charm.model.get_secret(label=label)
            secret.set_content(content=content)
        except ops.SecretNotFoundError:
            secret = charm.app.add_secret(label=label, content=content)

        return cls(secret)

    @classmethod
    def load(cls, charm: ops.CharmBase, label: str) -> Self | None:
        """Load a secret.

        Args:
            charm: Charm to load secret from.
            label: Secret label.
        """
        try:
            secret = charm.model.get_secret(label=label)
            return cls(secret)
        except ops.SecretNotFoundError:
            return None

    def grant(self, relation: ops.Relation) -> None:
        """Grant read access to this secret.

        Args:
            relation: Integration to grant read access to.
        """
        self._secret.grant(relation)

    def remove(self) -> None:
        """Remove all revisions of this secret."""
        self._secret.remove_all_revisions()


class _SlurmDataEncoder(json.JSONEncoder):
    """JSON encoder for Slurm-related data."""

    def default(self, o: Any) -> Any:
        # Encode a data model from `slurmutils`. `.dict()` will
        # perform the auto-expansion of the object into a raw dictionary.
        if isinstance(o, BaseModel):
            return o.dict()

        return super().default(o)


def _update_app_data(
    app: ops.Application, integration: ops.Relation, content: Mapping[str, Any]
) -> None:
    """Update an application's databag in an integration.

    Args:
        app: Application to update.
        integration: Integration holding application's databag.
        content: Content to update application databag with.

    Raises:
        ops.RelationDataError: Raised if non-leader unit attempts to update application data.
    """
    content = {k: json.dumps(v, cls=_SlurmDataEncoder) if v else "" for k, v in content.items()}
    integration.data[app].update(content)


def _update_unit_data(
    unit: ops.Unit, integration: ops.Relation, content: Mapping[str, Any]
) -> None:
    """Update a unit's databag in an integration.

    Args:
        unit: Unit to update.
        integration: Integration holding unit's databag.
        content: Content to update application databag with.
    """
    content = {k: json.dumps(v, cls=_SlurmDataEncoder) if v else "" for k, v in content.items()}
    integration.data[unit].update(content)


def _get_node_data(
    unit: ops.Unit, integration: ops.Relation
) -> tuple[Node, GRESNodeMapping | None]:
    """Gat all node and associated GRES data in a partition.

    Args:
        unit: Unit (node) instance to get data from.
        integration: Integration (partition) instance node is a member of.
    """
    node = integration.data[unit].get("node")
    gres = integration.data[unit].get("gres")

    return Node.from_json(node), GRESNodeMapping.from_json(gres) if gres else None


def _get_partition_data(
    integration: ops.Relation,
) -> tuple[Partition, dict[str, Node], GRESNodeMapping]:
    """Get partition data from a `slurmd` integration databag.

    Args:
        integration: Integration (partition) instance to get application data from.
    """
    node_mapping = {}
    gres_mapping = GRESNodeMapping()

    for unit in integration.units:
        node_data, gres_data = _get_node_data(unit, integration)
        node_mapping[node_data.node_name] = node_data
        if gres_data:
            gres_mapping.update(gres_data)

    # FIXME: This is disgusting. Add proper expansion capabilities to `slurmutils`.
    partition = Partition.from_json(integration.data[integration.app].get("partition"))
    partition.nodes = [node.dict() for node in node_mapping.values()]

    return partition, node_mapping, gres_mapping


# TODO: Some additional deserialization is required here.
#   Maybe `__post_init__` is required here, maybe not.
#   Ideally everything will come in as a JSON encoded string.


@dataclass
class SlurmctldProviderData:
    """`slurmctld` application data."""

    auth_key: str
    auth_key_id: str | None = None
    hostnames: Iterable[str] | None = None
    jwt_key: str | None = None
    jwt_key_id: str | None = None
    slurm_config: SlurmConfig | None = None


@dataclass
class SlurmdProviderData:
    """Data provided by the `slurmd` application and units."""

    slurm_config: SlurmConfig
    gres_config: GRESConfig


@dataclass
class SlurmdbdProviderData:
    """`slurmdbd` application data."""

    hostname: str


class SackdConnectedEvent(ops.RelationEvent):
    """Event emitted when `sackd` is connected to `slurmctld`."""


class SlurmctldConnectedEvent(ops.RelationEvent):
    """Event emitted when `slurmctld` is connected to a Slurm-related service."""


class SlurmctldReadyEvent(ops.RelationEvent):
    """Event emitted when the primary `slurmctld` service is ready.

    Notes:
        - `slurmctld` is "ready" once it is fully initialized and able to share
           all the required information needed by another Slurm-related service.
    """


class SlurmctldUnavailableEvent(ops.RelationEvent):
    """Event emitted when the `slurmctld` service is unavailable."""


class SlurmdConnectedEvent(ops.RelationEvent):
    """Event emitted when `slurmd` is connected to `slurmctld`."""


class SlurmdReadyEvent(ops.RelationEvent):
    """Event emitted when a `slurmd` service is ready.

    Notes:
        - `slurmd` is "ready" once a unit is fully initialized and able to
           share node and partition information required by `slurmctld`.
    """


class SlurmdDepartedEvent(ops.RelationEvent):
    """Event emitted when a single `slurmd` unit departs.

    Notes:
        - A `slurmd` unit is considered departed when an individual unit
          is removed from a partition.
    """


class SlurmdUnavailableEvent(ops.RelationEvent):
    """Event emitted when `slurmd` is unavailable.

    Notes:
        - A `slurmd` application (partition) is considered unavailable when
          the application is disconnected from `slurmctld`.
    """


class SlurmdbdConnectedEvent(ops.RelationEvent):
    """Event emitted when `slurmdbd` is connected to `slurmctld`."""


class SlurmdbdReadyEvent(ops.RelationEvent):
    """Event emitted when the primary `slurmdbd` service is ready.

    Notes:
        - `slurmdbd` is "ready" once it is fully initialized and able to share
           all the required information needed by `slurmctld`.
    """


class SlurmdbdUnavailableEvent(ops.RelationEvent):
    """Event emitted when `slurmdbd` is unavailable."""


class SlurmrestdConnectedEvent(ops.RelationEvent):
    """Event emitted when `slurmrestd` is connected to `slurmctld`."""


class _SlurmctldRequirerEvents(ops.ObjectEvents):
    """`slurmctld` requirer events."""

    slurmctld_connected = ops.EventSource(SlurmctldConnectedEvent)
    slurmctld_ready = ops.EventSource(SlurmctldReadyEvent)
    slurmctld_unavailable = ops.EventSource(SlurmctldUnavailableEvent)


class SackdRequirerEvents(ops.ObjectEvents):
    """`sackd` requirer events."""

    sackd_connected = ops.EventSource(SackdConnectedEvent)


class SlurmdRequirerEvents(ops.ObjectEvents):
    """`slurmd` requirer events."""

    slurmd_connected = ops.EventSource(SlurmdConnectedEvent)
    slurmd_ready = ops.EventSource(SlurmdReadyEvent)
    slurmd_departed = ops.EventSource(SlurmdDepartedEvent)
    slurmd_unavailable = ops.EventSource(SlurmdUnavailableEvent)


class SlurmdbdRequirerEvents(ops.ObjectEvents):
    """`slurmdbd` requirer events."""

    slurmdbd_connected = ops.EventSource(SlurmdbdConnectedEvent)
    slurmdbd_ready = ops.EventSource(SlurmdbdReadyEvent)
    slurmdbd_unavailable = ops.EventSource(SlurmdbdUnavailableEvent)


class SlurmrestdRequirerEvents(ops.ObjectEvents):
    """`slurmrestd` requirer events."""

    slurmrestd_connected = ops.EventSource(SlurmrestdConnectedEvent)


class _BaseInterface(ops.Object):
    """Base interface for Slurm-related integrations."""

    def __init__(self, charm: ops.CharmBase, integration_name: str) -> None:
        super().__init__(charm, integration_name)
        self.charm = charm
        self.app = charm.app
        self.unit = charm.unit
        self._integration_name = integration_name

    @property
    def integrations(self) -> list[ops.Relation]:
        """Get list of integration instances associated with the configured integration name."""
        return [
            integration
            for integration in self.charm.model.relations[self._integration_name]
            if self._is_integration_active(integration)
        ]

    def get_integration(self, integration_id: int | None = None) -> ops.Relation:
        """Get integration instance.

        Args:
            integration_id:
                ID of integration instance to retrieve. Required if there are
                multiple integrations of the same name in Juju's database.
                For example, you must pass the integration ID if multiple
                `slurmd` partitions exist.
        """
        return self.charm.model.get_relation(self._integration_name, integration_id)

    @staticmethod
    def _is_integration_active(integration: ops.Relation) -> bool:
        """Check if an integration is active by accessing contained data."""
        try:
            _ = repr(integration.data)
            return True
        except (RuntimeError, ops.ModelError):
            return False


class _SlurmctldRequirer(_BaseInterface):
    """Base interface for Slurm service providers to retrieve data provided by `slurmctld`."""

    on = _SlurmctldRequirerEvents()

    def __init__(self, charm: ops.CharmBase, integration_name: str) -> None:
        super().__init__(charm, integration_name)

        self.framework.observe(
            self.charm.on[self._integration_name].relation_created,
            self._on_relation_created,
        )
        self.framework.observe(
            self.charm.on[self._integration_name].relation_changed,
            self._on_relation_changed,
        )
        self.framework.observe(
            self.charm.on[self._integration_name].relation_broken,
            self._on_relation_broken,
        )

    def _on_relation_created(self, event: ops.RelationCreatedEvent) -> None:
        """Handle when `slurmctld` is connected."""
        self.on.slurmctld_connected.emit(event.relation)

    def _on_relation_changed(self, event: ops.RelationChangedEvent) -> None:
        """Handle when data from the primary `slurmctld` unit is ready."""
        provider_app = event.app
        if not event.relation.data.get(provider_app):
            return

        self.on.slurmctld_ready.emit(event.relation)

    def _on_relation_broken(self, event: ops.RelationBrokenEvent) -> None:
        """Handle when the integration with `slurmctld` is broken."""
        self.on.slurmctld_unavailable.emit(event.relation)

    def get_controller_data(
        self, integration: ops.Relation | None = None, integration_id: int | None = None
    ) -> SlurmctldProviderData | None:
        """Get controller data from the `slurmctld` application databag."""
        if not integration:
            integration = self.charm.model.get_relation(self._integration_name, integration_id)

        if not integration:
            return

        provider_app_data = dict(integration.data.get(integration.app))
        if auth_key_id := provider_app_data.get("auth_key_id"):
            auth_key = self.charm.model.get_secret(id=auth_key_id)
            provider_app_data["auth_key"] = auth_key.get_content().get("key")
        if jwt_key_id := provider_app_data.get("jwt_key_id"):
            jwt_key = self.charm.model.get_secret(id=jwt_key_id)
            provider_app_data["jwt_key"] = jwt_key.get_content().get("key")

        return SlurmctldProviderData(**provider_app_data) if provider_app_data else None

    def controller_ready(self, integration_id: int | None = None) -> bool:
        """Check if controller resources are ready.

        Args:
            integration_id: ID of integration to check.
        """
        if integration_id is None:
            return (
                all(
                    self._controller_ready_for_integration(integration)
                    for integration in self.integrations
                )
                if self.integrations
                else False
            )

        integration = self.get_integration(integration_id)
        return self._controller_ready_for_integration(integration)

    @staticmethod
    def _controller_ready_for_integration(integration: ops.Relation) -> bool:
        """Check if the `auth_key` and `hostnames` resources have been created.

        Args:
            integration: Integration to check.

        Notes:
            - This method should be overridden if controller requirer requires additional
              resources from the controller
        """
        if not integration.app:
            return False

        return (
            "auth_key" in integration.data[integration.app]
            and "hostnames" in integration.data[integration.app]
        )


class SackdProvider(_SlurmctldRequirer):
    """Integration interface for `sackd` providers."""


class SlurmdProvider(_SlurmctldRequirer):
    """Integration interface for `slurmd` providers."""

    def set_node_data(
        self, node: Node, gres: GRESNodeMapping | None = None, integration_id: int | None = None
    ) -> None:
        """Set node and GRES data within a unit databag in the `slurmd` integration.

        Args:
            node: Node data to set.
            gres: Node-specific GRES data to set.
            integration_id: ID of integration to update.
        """
        _update_unit_data(
            self.unit,
            self.get_integration(integration_id),
            {"node": node, "gres": gres},
        )

    @leader
    def set_partition_data(self, partition: Partition, integration_id: int | None = None) -> None:
        """Set partition data within an application databag in the `slurmd` integration.

        Args:
            partition: Partition data to set
            integration_id: ID of integration to update.
        """
        _update_app_data(self.app, self.get_integration(integration_id), {"partition": partition})


class SlurmdbdProvider(_SlurmctldRequirer):
    """Integration interface for `slurmdbd` providers."""

    @leader
    def set_database_data(
        self, hostnames: Iterable[str], integration_id: int | None = None
    ) -> None:
        """Set database data for application on `slurmdbd` integration.

        Args:
            hostnames: List of `slurmdbd` service hostnames.
            integration_id: ID of integration to update
        """
        _update_app_data(
            self.app,
            self.get_integration(integration_id),
            {"hostnames": hostnames},
        )


class SlurmrestdProvider(_SlurmctldRequirer):
    """Integration interface for `slurmrestd` providers."""


class _SlurmctldProvider(_BaseInterface):

    def __init__(self, charm: ops.CharmBase, integration_name: str) -> None:
        super().__init__(charm, integration_name)

        self.framework.observe(
            self.charm.on[self._integration_name].relation_broken,
            self._on_relation_broken,
        )

    @leader
    def _on_relation_broken(self, event: ops.RelationBrokenEvent) -> None:
        """Revoke departing application's access to Slurm secrets."""
        if auth_secret := _Secret.load(
            self.charm,
            label=AUTH_KEY_TEMPLATE_LABEL.substitute(id=event.relation.id),
        ):
            auth_secret.remove()

        if jwt_secret := _Secret.load(
            self.charm,
            label=JWT_KEY_TEMPLATE_LABEL.substitute(id=event.relation.id),
        ):
            jwt_secret.remove()

    @leader
    def set_controller_data(
        self, content: SlurmctldProviderData, /, integration_id: int | None = None
    ) -> None:
        """Set `slurmctld` controller data for Slurm services on application databag.

        Args:
            content: `slurmctld` provider data to set on application databag.
            integration_id:
                Grant an integration access to Slurm secrets. This argument must not
                be set to the ID of an integration if that integration requires
                access to Slurm secrets.
        """
        integrations = self.charm.model.relations.get(self._integration_name)
        if not integrations:
            return

        if integration_id is not None:
            integrations = [
                integration for integration in integrations if integration.id == integration_id
            ]

            secret = _Secret.create_or_update(
                self.charm,
                AUTH_KEY_TEMPLATE_LABEL.substitute(id=integration_id),
                {"key": content.auth_key},
            )
            secret.grant(integrations[0])
            content.auth_key_id = secret.uri

            if content.jwt_key:
                secret.create_or_update(
                    self.charm,
                    JWT_KEY_TEMPLATE_LABEL.substitute(id=integration_id),
                    {"key": content.jwt_key},
                )
                secret.grant(integrations[0])
                content.jwt_key_id = secret.uri

        for integration in integrations:
            _update_app_data(self.app, integration, asdict(content))


class SackdRequirer(_SlurmctldProvider):
    """Interface for the Slurm controller to provide data to the sackd service."""

    on = SackdRequirerEvents()

    def __init__(self, charm: ops.CharmBase, integration_name: str) -> None:
        super().__init__(charm, integration_name)

        self.framework.observe(
            self.charm.on[self._integration_name].relation_created,
            self._on_relation_created,
        )

    @leader
    def _on_relation_created(self, event: ops.RelationCreatedEvent) -> None:
        """Handle when `sackd` is connected to `slurmctld`."""
        self.on.sackd_connected.emit(event.relation)


class SlurmdRequirer(_SlurmctldProvider):
    """Integration interface for `slurmd` requirers."""

    on = SlurmdRequirerEvents()

    def __init__(self, charm: ops.CharmBase, integration_name: str) -> None:
        super().__init__(charm, integration_name)

        self.framework.observe(
            self.charm.on[self._integration_name].relation_created,
            self._on_relation_created,
        )
        self.framework.observe(
            self.charm.on[self._integration_name].relation_changed,
            self._on_relation_changed,
        )
        self.framework.observe(
            self.charm.on[self._integration_name].relation_departed,
            self._on_relation_departed,
        )
        self.framework.observe(
            self.charm.on[self._integration_name].relation_broken,
            self._on_relation_broken,
        )

    @leader
    def _on_relation_created(self, event: ops.RelationCreatedEvent) -> None:
        """Handle when `slurmd` is connected to `slurmctld`."""
        self.on.slurmd_connected.emit(event.relation)

    @leader
    def _on_relation_changed(self, event: ops.RelationChangedEvent) -> None:
        """Handle when data from a `slurmd` unit is ready."""
        if not event.relation.data.get(event.app) and not event.relation.data.get(event.unit):
            return

        self.on.slurmd_ready.emit(event.relation)

    @leader
    def _on_relation_departed(self, event: ops.RelationDepartedEvent) -> None:
        """Handle when `slurmd` unit leaves integration."""
        self.on.slurmd_departed.emit(event.relation)

    @leader
    def _on_relation_broken(self, event: ops.RelationBrokenEvent) -> None:
        """Handle when `slurmd` integration is broken."""
        super()._on_relation_broken(event)
        self.on.slurmd_unavailable.emit(event.relation)

    def get_compute_data(self) -> SlurmdProviderData:
        """Get data for all nodes and partitions in current cluster."""
        slurm_config = SlurmConfig()
        gres_config = GRESConfig()

        for partition in self.integrations:
            partition_data, node_data, gres_data = _get_partition_data(partition)
            # FIXME: This is disgusting. `slurmutils` needs proper mapping objects.
            slurm_config.partitions[partition_data.partition_name] = partition_data.dict()
            slurm_config.nodes.update({k: v.dict() for k, v in node_data.items()})
            gres_config.nodes.update(gres_data)

        return SlurmdProviderData(slurm_config, gres_config)


class SlurmdbdRequirer(_SlurmctldProvider):
    """Integration interface for `slurmdbd` requirers."""

    on = SlurmdbdRequirerEvents()

    def __init__(self, charm: ops.CharmBase, integration_name: str) -> None:

        super().__init__(charm, integration_name)

        self.framework.observe(
            self.charm.on[self._integration_name].relation_created,
            self._on_relation_created,
        )
        self.framework.observe(
            self.charm.on[self._integration_name].relation_changed,
            self._on_relation_changed,
        )

    @leader
    def _on_relation_created(self, event: ops.RelationCreatedEvent) -> None:
        """Handle when `slurmdbd` is connected to `slurmctld`."""
        self.on.slurmdbd_connected.emit(event.relation)

    @leader
    def _on_relation_changed(self, event: ops.RelationChangedEvent) -> None:
        """Handle when data from the primary `slurmdbd` unit is ready."""
        provider_app = event.app
        if not event.relation.data.get(provider_app):
            return

        self.on.slurmdbd_ready.emit(event.relation)

    @leader
    def _on_relation_broken(self, event: ops.RelationBrokenEvent) -> None:
        """Handle when integration with `slurmdbd` is broken."""
        super()._on_relation_broken(event)
        self.on.slurmdbd_unavailable.emit(event.relation)

    def get_database_data(
        self, integration: ops.Relation | None = None, integration_id: int | None = None
    ) -> SlurmdbdProviderData | None:
        """Get database data from the `slurmdbd` application databag."""
        if not integration:
            integration = self.charm.model.get_relation(self._integration_name, integration_id)

        if not integration:
            return

        provider_app_data = dict(integration.data.get(integration.app))
        return SlurmdbdProviderData(**provider_app_data) if provider_app_data else None


class SlurmrestdRequirer(_SlurmctldProvider):
    """Integration interface for `slurmrestd` requirers."""

    on = SlurmrestdRequirerEvents()

    def __init__(self, charm: ops.CharmBase, integration_name: str) -> None:
        super().__init__(charm, integration_name)

        self.framework.observe(
            self.charm.on[self._integration_name].relation_created,
            self._on_relation_created,
        )

    @leader
    def _on_relation_created(self, event: ops.RelationCreatedEvent) -> None:
        """Handle when `slurmrestd` is connected to `slurmctld`."""
        self.on.slurmrestd_connected.emit(event.relation)
