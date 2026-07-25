"""
Fleet telemetry parser for architecture diagrams.

Parses Insights Operator telemetry tarballs (gzipped, no extension) and
produces the same data dict as openshift_mg.py for the diagram renderer.

Telemetry layout:
    <customer_dir>/
        <cluster-uuid>/
            YYYYMMDDHHMMSS-hexdigest   (gzip tarball, latest picked by sort)

Internal tarball paths:
    config/version.json          -> ClusterVersion
    config/infrastructure.json   -> Infrastructure
    config/network.json          -> Network config
    config/ingress.json          -> Ingress config
    config/node/<name>.json      -> Node objects (no logs subdirectory)
    config/storage/storageclasses/<name>.json -> StorageClasses
"""

from __future__ import annotations

import json
import os
import re
import tarfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from openshift_mg import (
    apply_display_names,
    bucket_nodes,
    classify_roles,
    extract_ingress_info,
    extract_network_info,
    parse_memory_to_gib,
    short_hostname,
    _node_ready,
    _internal_ip,
)

TELEMETRY_PATHS = {
    "version": "config/version.json",
    "infrastructure": "config/infrastructure.json",
    "network": "config/network.json",
    "ingress": "config/ingress.json",
    "nodes_prefix": "config/node/",
    "storage_prefix": "config/storage/storageclasses/",
}

_TARBALL_RE = re.compile(r"^\d{14}-[0-9a-f]{32}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _read_json(tar: tarfile.TarFile, member_name: str) -> Optional[dict]:
    try:
        member = tar.getmember(member_name)
        f = tar.extractfile(member)
        if f is None:
            return None
        return json.load(f)
    except (KeyError, json.JSONDecodeError, tarfile.TarError):
        return None


def _find_latest_tarball(cluster_dir: str) -> Optional[str]:
    """Find the most recent telemetry tarball in a cluster UUID directory."""
    candidates = []
    for entry in os.listdir(cluster_dir):
        if _TARBALL_RE.match(entry):
            full = os.path.join(cluster_dir, entry)
            if os.path.isfile(full):
                candidates.append(full)
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0]


def _extract_nodes_from_tar(tar: tarfile.TarFile) -> List[dict]:
    """Extract node objects from config/node/*.json (skip logs subdirectory)."""
    nodes: List[dict] = []
    prefix = TELEMETRY_PATHS["nodes_prefix"]
    for member in tar.getnames():
        if not member.startswith(prefix):
            continue
        if not member.endswith(".json"):
            continue
        # Skip config/node/logs/ subdirectory
        relative = member[len(prefix):]
        if "/" in relative:
            continue
        node_doc = _read_json(tar, member)
        if not node_doc:
            continue

        meta = node_doc.get("metadata") or {}
        status = node_doc.get("status") or {}
        name = meta.get("name")
        if not name:
            continue

        roles = [
            key.replace("node-role.kubernetes.io/", "")
            for key in (meta.get("labels") or {})
            if key.startswith("node-role.kubernetes.io/")
        ]
        if not roles:
            roles = ["node"]

        bucket, primary, secondary = classify_roles(roles)
        capacity = status.get("capacity") or {}
        allocatable = status.get("allocatable") or {}
        node_info = status.get("nodeInfo") or {}

        nodes.append(
            {
                "name": name,
                "short_name": short_hostname(name),
                "roles": roles,
                "bucket": bucket,
                "primary_role": primary,
                "secondary_roles": secondary,
                "cpu": capacity.get("cpu", "N/A"),
                "memory": parse_memory_to_gib(capacity.get("memory", "N/A")),
                "cpu_allocatable": allocatable.get("cpu", "N/A"),
                "memory_allocatable": parse_memory_to_gib(
                    allocatable.get("memory", "N/A")
                ),
                "ready": _node_ready(status),
                "internal_ip": _internal_ip(status),
                "os_image": node_info.get("osImage", "N/A"),
                "architecture": node_info.get("architecture", "N/A"),
                "kubelet": node_info.get("kubeletVersion", "N/A"),
            }
        )

    nodes.sort(key=lambda n: (n["bucket"], n["short_name"]))
    return nodes


def _extract_storage_classes_from_tar(tar: tarfile.TarFile) -> List[dict]:
    """Extract StorageClass objects from config/storage/storageclasses/*.json."""
    storage_classes: List[dict] = []
    prefix = TELEMETRY_PATHS["storage_prefix"]
    for member in tar.getnames():
        if not member.startswith(prefix):
            continue
        if not member.endswith(".json"):
            continue
        sc_doc = _read_json(tar, member)
        if not sc_doc:
            continue

        meta = sc_doc.get("metadata") or {}
        annotations = meta.get("annotations") or {}
        is_default = (
            annotations.get("storageclass.kubernetes.io/is-default-class") == "true"
        )
        storage_classes.append(
            {
                "name": meta.get("name", "N/A"),
                "provisioner": sc_doc.get("provisioner", "N/A"),
                "default": is_default,
                "reclaim_policy": sc_doc.get("reclaimPolicy", "N/A"),
            }
        )

    storage_classes.sort(key=lambda s: (not s["default"], s["name"]))
    return storage_classes


def _derive_console_url(api_url: str) -> str:
    """Derive console URL from API URL when console config is not in telemetry."""
    if not api_url or api_url == "N/A":
        return "N/A"
    # https://api.cluster.example.com:6443 -> https://console-openshift-console.apps.cluster.example.com
    try:
        from urllib.parse import urlparse
        parsed = urlparse(api_url)
        hostname = parsed.hostname or ""
        if hostname.startswith("api."):
            apps_host = hostname[4:]
            return f"https://console-openshift-console.apps.{apps_host}"
    except Exception:
        pass
    return "N/A"


def _derive_cluster_name(infra_name: str, api_url: str, cluster_id: str) -> str:
    """Pick the best cluster name, handling telemetry redaction."""
    if infra_name and infra_name != "N/A" and "xxxxx" not in infra_name:
        return infra_name
    # Derive from API URL hostname
    if api_url and api_url != "N/A":
        try:
            from urllib.parse import urlparse
            hostname = urlparse(api_url).hostname or ""
            if hostname.startswith("api."):
                return hostname[4:]
        except Exception:
            pass
    # Fall back to cluster UUID
    if cluster_id and cluster_id != "N/A":
        return cluster_id[:13]
    return "Unknown Cluster"


def analyze_telemetry_tarball(tarball_path: str) -> Optional[dict]:
    """Parse a single telemetry tarball into a diagram-ready data dict."""
    data: Dict[str, Any] = {
        "cluster_name": "Unknown Cluster",
        "cluster_id": "N/A",
        "version": "N/A",
        "channel": "N/A",
        "version_state": "N/A",
        "platform": "N/A",
        "api_url": "N/A",
        "api_internal_url": "N/A",
        "console_url": "N/A",
        "control_plane_topology": "N/A",
        "infrastructure_topology": "N/A",
        "network": {},
        "ingress": {},
        "nodes": [],
        "buckets": {
            "control-plane": [],
            "infra": [],
            "worker": [],
            "other": [],
        },
        "storage_classes": [],
    }

    try:
        tar = tarfile.open(tarball_path, "r:gz")
    except (tarfile.TarError, OSError) as e:
        print(f"Error opening telemetry tarball {tarball_path}: {e}")
        return None

    with tar:
        # ClusterVersion
        version_doc = _read_json(tar, TELEMETRY_PATHS["version"])
        if version_doc:
            status = version_doc.get("status") or {}
            desired = status.get("desired") or {}
            data["version"] = desired.get("version") or status.get("version") or "N/A"
            data["cluster_id"] = (version_doc.get("spec") or {}).get("clusterID", "N/A")
            data["channel"] = (version_doc.get("spec") or {}).get("channel", "N/A")
            history = status.get("history") or []
            if history:
                data["version_state"] = history[0].get("state", "N/A")
            for cond in status.get("conditions") or []:
                if cond.get("type") == "Available" and cond.get("status") == "True":
                    data["version_state"] = data["version_state"] or "Completed"
                    break

        # Infrastructure
        infra_doc = _read_json(tar, TELEMETRY_PATHS["infrastructure"])
        if infra_doc:
            status = infra_doc.get("status") or {}
            spec = infra_doc.get("spec") or {}
            platform = status.get("platform") or (spec.get("platformSpec") or {}).get("type", "N/A")
            data["platform"] = platform or "N/A"
            raw_name = status.get("infrastructureName") or ""
            data["api_url"] = status.get("apiServerURL") or "N/A"
            data["api_internal_url"] = status.get("apiServerInternalURI") or "N/A"
            data["control_plane_topology"] = status.get("controlPlaneTopology") or "N/A"
            data["infrastructure_topology"] = status.get("infrastructureTopology") or "N/A"
            data["cluster_name"] = _derive_cluster_name(raw_name, data["api_url"], data["cluster_id"])

        # Console URL (not in telemetry, derive from API URL)
        data["console_url"] = _derive_console_url(data["api_url"])

        # Network
        network_doc = _read_json(tar, TELEMETRY_PATHS["network"])
        data["network"] = extract_network_info(network_doc)

        # Ingress
        ingress_doc = _read_json(tar, TELEMETRY_PATHS["ingress"])
        data["ingress"] = extract_ingress_info(ingress_doc)

        # Nodes
        data["nodes"] = _extract_nodes_from_tar(tar)

        # Storage classes
        data["storage_classes"] = _extract_storage_classes_from_tar(tar)

    apply_display_names(data["nodes"], data.get("cluster_name"))
    data["buckets"] = bucket_nodes(data["nodes"])

    return data


def analyze_telemetry(cluster_dir: str) -> Optional[dict]:
    """Parse the latest telemetry snapshot for one cluster.

    Args:
        cluster_dir: Path to a cluster UUID directory containing telemetry tarballs,
                     or a direct path to a single telemetry tarball file.
    """
    p = Path(cluster_dir).expanduser()

    if p.is_file():
        print(f"Analyzing telemetry tarball: {p}")
        return analyze_telemetry_tarball(str(p))

    if not p.is_dir():
        print(f"Error: Path not found: {p}")
        return None

    tarball = _find_latest_tarball(str(p))
    if not tarball:
        print(f"Error: No telemetry tarballs found in {p}")
        return None

    print(f"Analyzing telemetry: {p.name} (latest: {Path(tarball).name})")
    return analyze_telemetry_tarball(tarball)


def discover_clusters(telemetry_dir: str) -> List[str]:
    """List cluster UUID subdirectories in a customer-level telemetry directory."""
    p = Path(telemetry_dir).expanduser()
    if not p.is_dir():
        return []
    clusters = []
    for entry in sorted(os.listdir(str(p))):
        if _UUID_RE.match(entry) and (p / entry).is_dir():
            clusters.append(entry)
    return clusters


def is_telemetry_cluster_dir(path: str) -> bool:
    """Check if a directory looks like a cluster telemetry dir (contains timestamp-hex files)."""
    p = Path(path).expanduser()
    if not p.is_dir():
        return False
    for entry in os.listdir(str(p)):
        if _TARBALL_RE.match(entry):
            return True
    return False


def is_telemetry_fleet_dir(path: str) -> bool:
    """Check if a directory looks like a fleet telemetry dir (contains UUID subdirs)."""
    p = Path(path).expanduser()
    if not p.is_dir():
        return False
    for entry in os.listdir(str(p)):
        if _UUID_RE.match(entry) and (p / entry).is_dir():
            return True
    return False
