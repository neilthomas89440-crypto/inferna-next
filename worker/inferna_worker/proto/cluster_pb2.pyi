from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class RegisterRequest(_message.Message):
    __slots__ = ("cluster_token", "hostname", "worker_name", "cluster_name", "version", "protocol_version")
    CLUSTER_TOKEN_FIELD_NUMBER: _ClassVar[int]
    HOSTNAME_FIELD_NUMBER: _ClassVar[int]
    WORKER_NAME_FIELD_NUMBER: _ClassVar[int]
    CLUSTER_NAME_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    cluster_token: str
    hostname: str
    worker_name: str
    cluster_name: str
    version: str
    protocol_version: int
    def __init__(self, cluster_token: _Optional[str] = ..., hostname: _Optional[str] = ..., worker_name: _Optional[str] = ..., cluster_name: _Optional[str] = ..., version: _Optional[str] = ..., protocol_version: _Optional[int] = ...) -> None: ...

class RegisterResponse(_message.Message):
    __slots__ = ("worker_id", "worker_token", "sync_interval_seconds")
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    WORKER_TOKEN_FIELD_NUMBER: _ClassVar[int]
    SYNC_INTERVAL_SECONDS_FIELD_NUMBER: _ClassVar[int]
    worker_id: str
    worker_token: str
    sync_interval_seconds: int
    def __init__(self, worker_id: _Optional[str] = ..., worker_token: _Optional[str] = ..., sync_interval_seconds: _Optional[int] = ...) -> None: ...

class GPUInfo(_message.Message):
    __slots__ = ("index", "vendor", "name", "vram_mb", "used_vram_mb", "utilization_pct", "uuid", "driver_version")
    INDEX_FIELD_NUMBER: _ClassVar[int]
    VENDOR_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    VRAM_MB_FIELD_NUMBER: _ClassVar[int]
    USED_VRAM_MB_FIELD_NUMBER: _ClassVar[int]
    UTILIZATION_PCT_FIELD_NUMBER: _ClassVar[int]
    UUID_FIELD_NUMBER: _ClassVar[int]
    DRIVER_VERSION_FIELD_NUMBER: _ClassVar[int]
    index: int
    vendor: str
    name: str
    vram_mb: int
    used_vram_mb: int
    utilization_pct: int
    uuid: str
    driver_version: str
    def __init__(self, index: _Optional[int] = ..., vendor: _Optional[str] = ..., name: _Optional[str] = ..., vram_mb: _Optional[int] = ..., used_vram_mb: _Optional[int] = ..., utilization_pct: _Optional[int] = ..., uuid: _Optional[str] = ..., driver_version: _Optional[str] = ...) -> None: ...

class SystemInfo(_message.Message):
    __slots__ = ("cpu_cores", "memory_mb", "os", "hostname")
    CPU_CORES_FIELD_NUMBER: _ClassVar[int]
    MEMORY_MB_FIELD_NUMBER: _ClassVar[int]
    OS_FIELD_NUMBER: _ClassVar[int]
    HOSTNAME_FIELD_NUMBER: _ClassVar[int]
    cpu_cores: int
    memory_mb: int
    os: str
    hostname: str
    def __init__(self, cpu_cores: _Optional[int] = ..., memory_mb: _Optional[int] = ..., os: _Optional[str] = ..., hostname: _Optional[str] = ...) -> None: ...

class InstanceStatus(_message.Message):
    __slots__ = ("instance_id", "state", "detail", "port", "generation")
    INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    PORT_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    instance_id: str
    state: str
    detail: str
    port: int
    generation: int
    def __init__(self, instance_id: _Optional[str] = ..., state: _Optional[str] = ..., detail: _Optional[str] = ..., port: _Optional[int] = ..., generation: _Optional[int] = ...) -> None: ...

class EngineConfig(_message.Message):
    __slots__ = ("engine", "model_name", "profile", "gpu_indexes", "vram_required_mb", "port", "requires_hf_token")
    ENGINE_FIELD_NUMBER: _ClassVar[int]
    MODEL_NAME_FIELD_NUMBER: _ClassVar[int]
    PROFILE_FIELD_NUMBER: _ClassVar[int]
    GPU_INDEXES_FIELD_NUMBER: _ClassVar[int]
    VRAM_REQUIRED_MB_FIELD_NUMBER: _ClassVar[int]
    PORT_FIELD_NUMBER: _ClassVar[int]
    REQUIRES_HF_TOKEN_FIELD_NUMBER: _ClassVar[int]
    engine: str
    model_name: str
    profile: str
    gpu_indexes: _containers.RepeatedScalarFieldContainer[int]
    vram_required_mb: int
    port: int
    requires_hf_token: bool
    def __init__(self, engine: _Optional[str] = ..., model_name: _Optional[str] = ..., profile: _Optional[str] = ..., gpu_indexes: _Optional[_Iterable[int]] = ..., vram_required_mb: _Optional[int] = ..., port: _Optional[int] = ..., requires_hf_token: _Optional[bool] = ...) -> None: ...

class InstanceCommand(_message.Message):
    __slots__ = ("instance_id", "action", "config", "generation")
    INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    instance_id: str
    action: str
    config: EngineConfig
    generation: int
    def __init__(self, instance_id: _Optional[str] = ..., action: _Optional[str] = ..., config: _Optional[_Union[EngineConfig, _Mapping]] = ..., generation: _Optional[int] = ...) -> None: ...

class SyncRequest(_message.Message):
    __slots__ = ("worker_id", "worker_token", "system", "gpus", "instances")
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    WORKER_TOKEN_FIELD_NUMBER: _ClassVar[int]
    SYSTEM_FIELD_NUMBER: _ClassVar[int]
    GPUS_FIELD_NUMBER: _ClassVar[int]
    INSTANCES_FIELD_NUMBER: _ClassVar[int]
    worker_id: str
    worker_token: str
    system: SystemInfo
    gpus: _containers.RepeatedCompositeFieldContainer[GPUInfo]
    instances: _containers.RepeatedCompositeFieldContainer[InstanceStatus]
    def __init__(self, worker_id: _Optional[str] = ..., worker_token: _Optional[str] = ..., system: _Optional[_Union[SystemInfo, _Mapping]] = ..., gpus: _Optional[_Iterable[_Union[GPUInfo, _Mapping]]] = ..., instances: _Optional[_Iterable[_Union[InstanceStatus, _Mapping]]] = ...) -> None: ...

class SyncResponse(_message.Message):
    __slots__ = ("commands",)
    COMMANDS_FIELD_NUMBER: _ClassVar[int]
    commands: _containers.RepeatedCompositeFieldContainer[InstanceCommand]
    def __init__(self, commands: _Optional[_Iterable[_Union[InstanceCommand, _Mapping]]] = ...) -> None: ...
