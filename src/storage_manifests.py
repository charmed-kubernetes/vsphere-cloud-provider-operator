# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
"""Implementation of vsphere specific details of the kubernetes manifests."""
import base64
import json
import logging
from hashlib import md5
from typing import Dict, Mapping, Optional

from ops.model import Relation

from manifests import (
    Addition,
    ApplyLabel,
    ApplyRegistry,
    CreateNamespace,
    Manifests,
    Patch,
)

log = logging.getLogger(__file__)
SECRET_NAME = "vsphere-config-secret"
SECRET_DATA = "csi-vsphere.conf"
STORAGE_CLASS_NAME = "csi-vsphere-{type}"


class UpdateDeployment(Patch):
    """Update the Deployment object to reference juju supplied node selector and replica."""

    def __call__(self, obj):
        """Apply control node selector and replica count to the deployment."""
        if not (
            obj.get("kind") == "Deployment" and obj["metadata"]["name"] == "vsphere-csi-controller"
        ):
            return
        node_selector = self.manifests.config.get("control-node-selector")
        if not isinstance(node_selector, dict):
            log.error(
                f"storage control-node-selector was an unexpected type: {type(node_selector)}"
            )
            return

        obj["spec"]["template"]["spec"]["nodeSelector"] = node_selector
        node_selector_text = " ".join('{0}: "{1}"'.format(*t) for t in node_selector.items())
        log.info(f"Applying storage Control Node Selector as {node_selector_text}")

        replicas = self.manifests.config.get("replicas")
        if not replicas:
            log.warning(f"Using storage default replicas of {obj['spec']['replicas']}")
            return
        obj["spec"]["replicas"] = replicas
        log.info(f"Setting storage deployment replicas to {replicas}")


class CreateSecret(Addition):
    """Create secret for the deployment."""

    def __call__(self) -> Optional[Dict]:
        """Craft the secrets object for the deployment."""
        secret = [
            self.manifests.config.get(k) for k in ("username", "password", "server", "datacenter")
        ]
        if any(s is None for s in secret):
            log.error("secret data item is None")
            return
        user, passwd, server, datacenter = secret
        log.info(f"Creating storage secret data for server {server}")
        secret_config = (
            f"[Global]\n"
            f'cluster-id = "{self.manifests.model_uuid}"\n'
            f"\n"
            f'[VirtualCenter "{server}"]\n'
            f'insecure-flag = "true"\n'
            f'user = "{user}"\n'
            f'password = "{passwd}"\n'
            f'port = "443"\n'
            f'datacenters = "{datacenter}"\n'
        ).encode()
        return dict(
            apiVersion="v1",
            kind="Secret",
            type="Opaque",
            metadata=dict(name=SECRET_NAME),
            data={SECRET_DATA: base64.b64encode(secret_config).decode("utf-8")},
        )


class CreateStorageClass(Addition):
    """Create vmware storage class."""

    def __init__(self, manifests: "Manifests", sc_type: str):
        super().__init__(manifests)
        self.type = sc_type

    def __call__(self) -> Optional[Dict]:
        """Craft the storage class object."""
        storage_name = STORAGE_CLASS_NAME.format(type=self.type)
        log.info(f"Creating storage class {storage_name}")
        return dict(
            apiVersion="storage.k8s.io/v1",
            kind="StorageClass",
            metadata=dict(
                name=storage_name,
                annotations={
                    "storageclass.kubernetes.io/is-default-class": "true",
                },
            ),
            provisioner="csi.vsphere.vmware.com",
            parameters=dict(storagepolicyname="vSAN Default Storage Policy"),
        )


class VsphereStorageManifests(Manifests):
    """Deployment Specific details for the vsphere-cloud-provider."""

    def __init__(
        self,
        charm_name: str,
        charm_config: Mapping[str, str],
        integrator,
        control_plane: Relation,
        kube_control,
        model_uuid: str,
    ):
        manipulations = [
            CreateNamespace(self),
            CreateSecret(self),
            ApplyLabel(self),
            ApplyRegistry(self),
            UpdateDeployment(self),
            CreateStorageClass(self, "default"),  # creates csi-vsphere-default
        ]
        super().__init__(
            charm_name,
            "upstream/cloud_storage",
            manipulations=manipulations,
            default_namespace="vmware-system-csi",
        )
        self.charm_config = charm_config
        self.integrator = integrator
        self.control_plane = control_plane
        self.kube_control = kube_control
        self.model_uuid = model_uuid

    @property
    def config(self) -> Dict:
        """Returns current config available from charm config and joined relations."""
        config = {}
        if self.integrator.is_ready:
            config = {
                "server": self.integrator.vsphere_ip,
                "username": self.integrator.user,
                "password": self.integrator.password,
                "datacenter": self.integrator.datacenter,
            }
        if self.kube_control.is_ready:
            config["image-registry"] = self.kube_control.registry_location

        if self.control_plane:
            config["control-node-selector"] = {"juju-application": self.control_plane.app.name}
            config["replicas"] = len(self.control_plane.units)

        config.update(**self.charm_config.available_data)

        for key, value in dict(**config).items():
            if value == "" or value is None:
                del config[key]

        config["release"] = config.pop("storage-release", None)

        return config

    def hash(self) -> int:
        """Calculate a hash of the current configuration."""
        return int(md5(json.dumps(self.config, sort_keys=True).encode("utf8")).hexdigest(), 16)

    def evaluate(self) -> str:
        """Determine if manifest_config can be applied to manifests."""
        props = ["server", "username", "password", "datacenter", "control-node-selector"]
        for prop in props:
            value = self.config.get(prop)
            if not value:
                return f"Storage manifests waiting for definition of {prop}"
