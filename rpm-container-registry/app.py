from __future__ import annotations

import logging
from typing import Any, Literal
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
import os.path
import json
import aiofiles

from obs_package_update.util import RunCommand

app = FastAPI()
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.DEBUG)
LOGGER.addHandler(logging.StreamHandler())

run_cmd = RunCommand(logger=LOGGER)

_OCI_BASE_PATH = "/usr/share/suse-docker-images/oci/"
_BLOBS_BASE_PATH = f"{_OCI_BASE_PATH}/blobs/sha256/"


class TagReply(BaseModel):
    name: str
    tags: list[str]


# class ManifestV1Reply(BaseModel):
#     class BlobSum(BaseModel):
#         blobSum: str

#     name: str
#     tag: str
#     fsLayers: list[BlobSum]
#     history: list[Any]

#     schemaVersion: Literal[1] = 1
#     # FIXME:
#     signature: Any = [{}]


class ChecksumMixin:
    @property
    def checksum(self) -> str:
        return self.digest.split(":")[1]


class DockerManifest(BaseModel, ChecksumMixin):
    class Platform(BaseModel):
        architecture: str
        os: str

    mediaType: Literal[
        "application/vnd.docker.distribution.manifest.v2+json"
    ] = "application/vnd.docker.distribution.manifest.v2+json"
    size: int
    digest: str
    platform: Platform


MANIFEST_LIST_V2_MEDIA_TYPE = (
    "application/vnd.docker.distribution.manifest.list.v2+json"
)


class ManifestListV2Reply(BaseModel):
    manifests: list[DockerManifest]

    schemaVersion: Literal[2] = 2
    mediaType: Literal[
        "application/vnd.docker.distribution.manifest.list.v2+json"
    ] = MANIFEST_LIST_V2_MEDIA_TYPE


class IndexJson(BaseModel):
    class Manifest(BaseModel, ChecksumMixin):
        mediaType: Literal["application/vnd.oci.image.manifest.v1+json"]
        digest: str
        size: int

    schemaVersion: Literal[2]
    manifests: list[Manifest]


MANIFEST_V2_MEDIA_TYPE = "application/vnd.docker.distribution.manifest.v2+json"


class ManifestV2Reply(BaseModel):
    class Config(BaseModel, ChecksumMixin):
        mediaType: Literal[
            "application/vnd.docker.container.image.v1+json"
        ] = "application/vnd.docker.container.image.v1+json"
        digest: str
        size: int

    class Layer(BaseModel, ChecksumMixin):
        mediaType: Literal[
            "application/vnd.docker.image.rootfs.diff.tar.gzip"
        ] = "application/vnd.docker.image.rootfs.diff.tar.gzip"
        digest: str
        size: int

    schemaVersion: Literal[2] = 2
    mediaType: Literal[
        "application/vnd.docker.distribution.manifest.v2+json"
    ] = MANIFEST_V2_MEDIA_TYPE
    config: Config
    layers: list[Layer]


OCI_IMAGE_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"


class OciManifest(BaseModel):
    class Config(BaseModel, ChecksumMixin):
        mediaType: Literal[
            "application/vnd.oci.image.config.v1+json"
        ] = OCI_IMAGE_CONFIG_MEDIA_TYPE
        digest: str
        size: int

    class Layer(BaseModel, ChecksumMixin):
        mediaType: Literal[
            "application/vnd.oci.image.layer.v1.tar+gzip"
        ] = "application/vnd.oci.image.layer.v1.tar+gzip"
        digest: str
        size: int

    schemaVersion: Literal[2] = 2
    mediaType: Literal["application/vnd.oci.image.manifest.v1+json"]
    config: Config
    layers: list[Layer]

    def to_docker_manifest(self) -> ManifestV2Reply:
        return ManifestV2Reply(
            config=ManifestV2Reply.Config(
                digest=self.config.digest, size=self.config.size
            ),
            layers=[
                ManifestV2Reply.Layer(digest=layer.digest, size=layer.size)
                for layer in self.layers
            ],
        )


class OciConfig(BaseModel):
    class HistoryEntry(BaseModel):
        created: str
        created_by: str | None = None
        empty_layer: bool | None = None
        comment: str | None = None

    history: list[HistoryEntry]

    created: str
    architecture: str
    os: str

    config: Any
    rootfs: Any


async def read_in_oci_image(
    image_name: str,
) -> tuple[IndexJson, OciManifest, OciConfig]:
    async with aiofiles.open(
        os.path.join(_OCI_BASE_PATH, image_name, "index.json"), "r"
    ) as index_json_f:
        index_json = IndexJson(**json.loads(await index_json_f.read()))

    async with aiofiles.open(
        os.path.join(
            _OCI_BASE_PATH,
            image_name,
            "blobs",
            "sha256",
            index_json.manifests[0].checksum,
        ),
        "r",
    ) as manifest_f:
        manifest = OciManifest(**json.loads(await manifest_f.read()))

    async with aiofiles.open(
        os.path.join(
            _OCI_BASE_PATH, image_name, "blobs", "sha256", manifest.config.checksum
        ),
        "r",
    ) as config_f:
        config = OciConfig(**json.loads(await config_f.read()))

    return index_json, manifest, config


async def package_names_from_rpm(
    image_name: str, img_tag: str | None = None
) -> list[str] | None:
    oci_img_provide = f"oci_image({image_name})"
    rpm_wp_res = await run_cmd(
        f"rpm -q --whatprovides '{oci_img_provide}'",
        raise_on_error=False,
    )
    if rpm_wp_res.exit_code != 0:
        return None

    pkgs = set(rpm_wp_res.stdout.splitlines())

    if not img_tag:
        return list(pkgs)

    res = []

    for pkg in pkgs:
        rpm_p_res = await run_cmd(f"rpm -qP {pkg}")
        for provides in rpm_p_res.stdout.splitlines():
            if "=" not in provides:
                continue

            capability, version = provides.split("=")
            if capability.strip() == oci_img_provide and version.strip() == img_tag:
                res.append(pkg)
                break

    return res


@app.get("/v2/")
async def get_base():
    """https://docs.docker.com/registry/spec/api/#get-base"""
    return {}


@app.head("/v2/{name}/manifests/{reference}")
async def check_manifest_exists(name: str, reference: str):
    # FIXME
    return {}


@app.get("/v2/{name:path}/tags/list")
async def send_tag_list(name: str) -> TagReply:
    """https://docs.docker.com/registry/spec/api/#listing-image-tags"""
    packages = await package_names_from_rpm(name)
    if not packages:
        raise HTTPException(status_code=404, detail=f"Image {name} not found")
    tags = []
    for pkg in packages:
        provides_res = await run_cmd(f"rpm -qP {pkg}")
        for prov in provides_res.stdout.splitlines():
            if (
                len(tmp := prov.split("=")) == 2
                and tmp[0].strip() == f"oci_image({name})"
            ):
                tags.append(tmp[1].strip())
    return TagReply(tags=tags, name=name)


@app.get("/v2/{name:path}/blobs/{digest}")
async def send_digest(name: str, digest: str):
    """https://docs.docker.com/registry/spec/api/#pulling-a-layer"""
    # this could be a request of a OciConfig and not a layer tarball
    oci_config_provides = await run_cmd(
        f"rpm -q --whatprovides 'oci_config({digest})'",
        raise_on_error=False,
    )
    if (
        oci_config_provides.exit_code == 0
        and len(pkgs := list(set(oci_config_provides.stdout.splitlines()))) == 1
    ):
        files = (await run_cmd(f"rpm -ql {pkgs[0]}")).stdout.splitlines()
        for fname in files:
            if fname.endswith(digest.replace("sha256:", "")):
                return FileResponse(path=fname, media_type=OCI_IMAGE_CONFIG_MEDIA_TYPE)

    if digest.startswith("sha256:"):
        digest = digest.replace("sha256:", "")

    if not os.path.exists((digest_file := os.path.join(_BLOBS_BASE_PATH, digest))):
        raise HTTPException(status_code=404, detail=f"Digest {digest} not found")

    return FileResponse(digest_file)


async def manifests_from_name(name: str, tag: str) -> list[DockerManifest]:
    pkgs = await package_names_from_rpm(name, tag)
    if not pkgs:
        raise HTTPException(status_code=404, detail=f"Image {name}:{tag} not found")

    manifests: list[DockerManifest] = []

    for pkg in pkgs:
        pkg_name, arch = (
            await run_cmd(f'rpm -q --qf "%{{name}} %{{arch}}" {pkg}')
        ).stdout.split(" ")

        arch = "amd64" if arch == "x86_64" else arch

        index_json, _, _ = await read_in_oci_image(pkg_name)

        manifests.extend(
            [
                DockerManifest(
                    size=manifest.size,
                    digest=manifest.digest,
                    platform=DockerManifest.Platform(architecture=arch, os="linux"),
                )
                for manifest in index_json.manifests
            ]
        )
    return manifests


async def manifests_from_sha_digest(digest: str) -> OciManifest | None:
    manifest_pkg_query = await run_cmd(
        f"rpm -q --whatprovides 'oci_manifest({digest})'",
        raise_on_error=False,
    )
    if manifest_pkg_query.exit_code != 0:
        return None

    if not (pkgs := set(manifest_pkg_query.stdout.splitlines())) or len(pkgs) != 1:
        raise ValueError(
            f"Got an invalid number of packages providing the digest {digest}: {pkgs}"
        )

    pkg_rpm_name = list(pkgs)[0]
    pkg_name = (await run_cmd(f'rpm -q --qf "%{{name}}" {pkg_rpm_name}')).stdout.strip()

    _, manifest, _ = await read_in_oci_image(pkg_name)
    return manifest


@app.get("/v2/{name:path}/manifests/{reference}")
async def read_manifest(name: str, reference: str) -> Response:
    """https://docs.docker.com/registry/spec/api/#pulling-an-image-manifest"""

    if reference.startswith("sha256:"):
        manifest = await manifests_from_sha_digest(reference)
        if not manifest:
            raise HTTPException(status_code=404, detail=f"digest {reference} not found")
        return Response(
            content=manifest.model_dump_json(),
            media_type=manifest.mediaType,
        )

    pkgs = await package_names_from_rpm(name, reference)
    if not pkgs:
        raise HTTPException(
            status_code=404, detail=f"Image {name}:{reference} not found"
        )

    manifests: list[DockerManifest] = []

    for pkg in pkgs:
        pkg_name, arch = (
            await run_cmd(f'rpm -q --qf "%{{name}} %{{arch}}" {pkg}')
        ).stdout.split(" ")

        arch = "amd64" if arch == "x86_64" else arch

        index_json, _, _ = await read_in_oci_image(pkg_name)

        manifests.extend(
            [
                DockerManifest(
                    size=manifest.size,
                    digest=manifest.digest,
                    platform=DockerManifest.Platform(architecture=arch, os="linux"),
                )
                for manifest in index_json.manifests
            ]
        )

    return Response(
        content=(ManifestListV2Reply(manifests=manifests)).model_dump_json(),
        # content=index_json.model_dump_json(),
        media_type=MANIFEST_LIST_V2_MEDIA_TYPE,
    )
