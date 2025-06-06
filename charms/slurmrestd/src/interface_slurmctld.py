"""Slurmctld interface for slurmrestd <-> slurmctld integration."""

import logging

from ops import (
    EventBase,
    EventSource,
    Object,
    ObjectEvents,
    RelationBrokenEvent,
    RelationChangedEvent,
)

logger = logging.getLogger()


class SlurmctldAvailableEvent(EventBase):
    """SlurmctldAvailableEvent."""

    def __init__(self, handle, auth_key, slurm_conf):
        super().__init__(handle)

        self.auth_key = auth_key
        self.slurm_conf = slurm_conf

    def snapshot(self):
        """Snapshot the event data."""
        return {
            "auth_key": self.auth_key,
            "slurm_conf": self.slurm_conf,
        }

    def restore(self, snapshot):
        """Restore the snapshot of the event data."""
        self.auth_key = snapshot.get("auth_key")
        self.slurm_conf = snapshot.get("slurm_conf")


class SlurmctldUnavailableEvent(EventBase):
    """SlurmctldUnavailableEvent."""


class Events(ObjectEvents):
    """'slurmctld' interface Events."""

    slurmctld_available = EventSource(SlurmctldAvailableEvent)
    slurmctld_unavailable = EventSource(SlurmctldUnavailableEvent)


class Slurmctld(Object):
    """Slurmctld interface."""

    on = Events()  # pyright: ignore [reportIncompatibleMethodOverride, reportAssignmentType]

    def __init__(self, charm, relation_name):
        """Set the provides initial data."""
        super().__init__(charm, relation_name)

        self._charm = charm
        self._relation_name = relation_name

        self.framework.observe(
            self._charm.on[relation_name].relation_changed, self._on_relation_changed
        )
        self.framework.observe(
            self._charm.on[relation_name].relation_broken, self._on_relation_broken
        )

    @property
    def is_joined(self) -> bool:
        """Return True if self._relation is not None."""
        return True if self.framework.model.relations.get(self._relation_name) else False

    def _on_relation_changed(self, event: RelationChangedEvent) -> None:
        """Get the auth key and slurm_conf from slurmctld on relation changed."""
        if app := event.app:
            if event_app_data := event.relation.data.get(app):
                auth_key = event_app_data.get("auth_key")
                slurm_conf = event_app_data.get("slurm_conf")

                logger.debug(f"auth_key={auth_key}")
                logger.debug(f"slurm_conf={slurm_conf}")

                if auth_key and slurm_conf:
                    self.on.slurmctld_available.emit(auth_key, slurm_conf)
                else:
                    logger.debug("'auth_key' or 'slurm_conf' not in relation data.")
            else:
                logger.debug("application data not available in event databag.")
        else:
            logger.debug("application not available in relation data.")

    def _on_relation_broken(self, event: RelationBrokenEvent) -> None:
        """Emit slurmctld_unavailable when the relation is broken."""
        self.on.slurmctld_unavailable.emit()
