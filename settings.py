"""插件配置和统一资料字段。"""

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROFILE_FIELDS = (
    "birthday", "height_cm", "bust_cm", "waist_cm", "hips_cm", "cup",
    "birthplace", "blood_type",
)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retry_days: int = Field(default=3, ge=1, le=365, title="重试间隔（天）")
    max_attempts: int = Field(default=3, ge=1, le=100, title="最大尝试次数")
    subscribed_only: bool = Field(default=False, title="仅处理已订阅女优")
    request_interval_seconds: float = Field(default=1, ge=0.2, le=60, title="请求间隔（秒）")
    timeout_seconds: int = Field(default=20, ge=1, le=120, title="请求超时（秒）")

    @model_validator(mode="before")
    @classmethod
    def remove_retired_config(cls, value):
        if isinstance(value, dict):
            return {key: item for key, item in value.items() if key not in {"javbus_mapping", "target_fields"}}
        return value
