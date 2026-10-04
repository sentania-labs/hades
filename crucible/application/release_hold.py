"""The durable release hold set while a Hades merge commit has red CI."""

from crucible.domain.entities import ProviderSetting
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork

SETTING_NAME = "release.main_ci_hold"


def release_held(uow: UnitOfWork) -> bool:
    setting = uow.provider_settings.get(SETTING_NAME)
    return setting is not None and setting.document.get("held") is True


def set_release_hold(
    uow: UnitOfWork, clock: Clock, *, held: bool, document: dict[str, object]
) -> None:
    uow.provider_settings.put(
        ProviderSetting(
            name=SETTING_NAME,
            document={"held": held, **document},
            updated_at=clock.now(),
            updated_by="crucible",
            reason="main CI must be green before tagging",
        )
    )
