from typing import List, Optional

from pydantic import BaseModel


class SystemInfo(BaseModel):
    hostname: str
    cpu_percent: float
    mem_percent: float
    disk_percent: float
    uptime_seconds: float
    local_ip: Optional[str] = None


class TailscaleInfo(BaseModel):
    connected: bool = False
    backend_state: Optional[str] = None
    ips: List[str] = []
    dns_name: Optional[str] = None  # MagicDNS name, e.g. plex-server.tailnet-name.ts.net
    tailnet: Optional[str] = None
    exit_node: Optional[str] = None


class SshAuthInfo(BaseModel):
    recent_accepted_count: int = 0
    recent_failed_count: int = 0
    last_accepted_user: Optional[str] = None
    last_accepted_ip: Optional[str] = None
    last_accepted_at: Optional[str] = None
    last_failed_ip: Optional[str] = None
    last_failed_at: Optional[str] = None


class DockerContainerInfo(BaseModel):
    name: str
    image: str
    status: str
    started_at: Optional[str] = None
    cpu_percent: Optional[float] = None
    mem_percent: Optional[float] = None


class ClientReport(BaseModel):
    system: SystemInfo
    tailscale: Optional[TailscaleInfo] = None
    ssh_auth: Optional[SshAuthInfo] = None
    docker_containers: List[DockerContainerInfo] = []
    ssh_users: List[str] = []
    agent_version: str = "1.0.0"


class DeviceCreate(BaseModel):
    name: str
    notes: str = ""
    report_interval_seconds: int = 60
    offline_after_seconds: int = 150
