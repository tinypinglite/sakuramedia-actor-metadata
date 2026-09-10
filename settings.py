"""插件配置和统一资料字段。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ProfileField = Literal[
    "birthday", "height_cm", "bust_cm", "waist_cm", "hips_cm", "cup",
    "birthplace", "blood_type", "gender",
]
PROFILE_FIELDS = (
    "birthday", "height_cm", "bust_cm", "waist_cm", "hips_cm", "cup",
    "birthplace", "blood_type", "gender",
)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retry_days: int = Field(default=3, ge=1, le=365)
    max_attempts: int = Field(default=3, ge=1, le=100)
    target_fields: tuple[ProfileField, ...] = PROFILE_FIELDS
    subscribed_only: bool = False
    request_interval_seconds: float = Field(default=1, ge=0.2, le=60)
    timeout_seconds: int = Field(default=20, ge=1, le=120)

    @field_validator("target_fields")
    @classmethod
    def validate_targets(cls, value):
        if not value or len(set(value)) != len(value):
            raise ValueError("target_fields 必须非空且不重复")
        return value

    @model_validator(mode="before")
    @classmethod
    def remove_retired_source_config(cls, value):
        # 已安装版本可能保存了此配置；换源后不再使用旧站点映射。
        if isinstance(value, dict) and "javbus_mapping" in value:
            return {key: item for key, item in value.items() if key != "javbus_mapping"}
        return value
