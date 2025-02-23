"""
Generates a Docker Image to be used for invoking a function locally
"""
import uuid
import logging
import hashlib
from enum import Enum
from pathlib import Path

import sys
import platform
import docker

from samcli.commands.local.cli_common.user_exceptions import ImageBuildException
from samcli.commands.local.lib.exceptions import InvalidIntermediateImageError
from samcli.lib.utils.packagetype import ZIP, IMAGE
from samcli.lib.utils.stream_writer import StreamWriter
from samcli.lib.utils.tar import create_tarball
from samcli import __version__ as version


LOG = logging.getLogger(__name__)


class Runtime(Enum):
    nodejs10x = "nodejs10.x"
    nodejs12x = "nodejs12.x"
    nodejs14x = "nodejs14.x"
    python27 = "python2.7"
    python36 = "python3.6"
    python37 = "python3.7"
    python38 = "python3.8"
    ruby25 = "ruby2.5"
    ruby27 = "ruby2.7"
    java8 = "java8"
    java8al2 = "java8.al2"
    java11 = "java11"
    go1x = "go1.x"
    dotnetcore21 = "dotnetcore2.1"
    dotnetcore31 = "dotnetcore3.1"
    provided = "provided"
    providedal2 = "provided.al2"

    @classmethod
    def has_value(cls, value):
        """
        Checks if the enum has this value

        :param string value: Value to check
        :return bool: True, if enum has the value
        """
        return any(value == item.value for item in cls)


class LambdaImage:
    _LAYERS_DIR = "/opt"
    _INVOKE_REPO_PREFIX = "public.ecr.aws/sam/emulation"
    _SAM_CLI_REPO_NAME = "samcli/lambda"
    _RAPID_SOURCE_PATH = Path(__file__).parent.joinpath("..", "rapid").resolve()

    def __init__(self, layer_downloader, skip_pull_image, force_image_build, docker_client=None):
        """

        Parameters
        ----------
        layer_downloader samcli.local.layers.layer_downloader.LayerDownloader
            LayerDownloader to download layers locally
        skip_pull_image bool
            True if the image should not be pulled from DockerHub
        force_image_build bool
            True to download the layer and rebuild the image even if it exists already on the system
        docker_client docker.DockerClient
            Optional docker client object
        """
        self.layer_downloader = layer_downloader
        self.skip_pull_image = skip_pull_image
        self.force_image_build = force_image_build
        self.docker_client = docker_client or docker.from_env()

    def build(self, runtime, packagetype, image, layers, stream=None):
        """
        Build the image if one is not already on the system that matches the runtime and layers

        Parameters
        ----------
        runtime str
            Name of the Lambda runtime
        packagetype str
            Packagetype for the Lambda
        image str
            Pre-defined invocation image.
        layers list(samcli.commands.local.lib.provider.Layer)
            List of layers

        Returns
        -------
        str
            The image to be used (REPOSITORY:TAG)
        """
        image_name = None

        if packagetype == IMAGE:
            image_name = image
        elif packagetype == ZIP:
            image_name = f"{self._INVOKE_REPO_PREFIX}-{runtime}:latest"

        if not image_name:
            raise InvalidIntermediateImageError(f"Invalid PackageType, PackageType needs to be one of [{ZIP}, {IMAGE}]")

        if image:
            self.skip_pull_image = True

        # Default image tag to be the base image with a tag of 'rapid' instead of latest.
        # If the image name had a digest, removing the @ so that a valid image name can be constructed
        # to use for the local invoke image name.
        image_tag = f"{image_name.split(':')[0].replace('@', '')}:rapid-{version}"

        downloaded_layers = []

        if layers and packagetype == ZIP:
            downloaded_layers = self.layer_downloader.download_all(layers, self.force_image_build)

            docker_image_version = self._generate_docker_image_version(downloaded_layers, runtime)
            image_tag = f"{self._SAM_CLI_REPO_NAME}:{docker_image_version}"

        image_not_found = False

        # If we are not using layers, build anyways to ensure any updates to rapid get added
        try:
            self.docker_client.images.get(image_tag)
        except docker.errors.ImageNotFound:
            LOG.info("Image was not found.")
            image_not_found = True

        if (
            self.force_image_build
            or image_not_found
            or any(layer.is_defined_within_template for layer in downloaded_layers)
            or not runtime
        ):
            stream_writer = stream or StreamWriter(sys.stderr)
            stream_writer.write("Building image...")
            stream_writer.flush()
            self._build_image(image if image else image_name, image_tag, downloaded_layers, stream=stream_writer)

        return image_tag

    def get_config(self, image_tag):
        config = {}
        try:
            image = self.docker_client.images.get(image_tag)
            return image.attrs.get("Config")
        except docker.errors.ImageNotFound:
            return config

    @staticmethod
    def _generate_docker_image_version(layers, runtime):
        """
        Generate the Docker TAG that will be used to create the image

        Parameters
        ----------
        layers list(samcli.commands.local.lib.provider.Layer)
            List of the layers

        runtime str
            Runtime of the image to create

        Returns
        -------
        str
            String representing the TAG to be attached to the image
        """

        # Docker has a concept of a TAG on an image. This is plus the REPOSITORY is a way to determine
        # a version of the image. We will produced a TAG for a combination of the runtime with the layers
        # specified in the template. This will allow reuse of the runtime and layers across different
        # functions that are defined. If two functions use the same runtime with the same layers (in the
        # same order), SAM CLI will only produce one image and use this image across both functions for invoke.
        return (
            runtime + "-" + hashlib.sha256("-".join([layer.name for layer in layers]).encode("utf-8")).hexdigest()[0:25]
        )

    def _build_image(self, base_image, docker_tag, layers, stream=None):
        """
        Builds the image

        Parameters
        ----------
        base_image str
            Base Image to use for the new image
        docker_tag
            Docker tag (REPOSITORY:TAG) to use when building the image
        layers list(samcli.commands.local.lib.provider.Layer)
            List of Layers to be use to mount in the image

        Raises
        ------
        samcli.commands.local.cli_common.user_exceptions.ImageBuildException
            When docker fails to build the image
        """
        dockerfile_content = self._generate_dockerfile(base_image, layers)

        # Create dockerfile in the same directory of the layer cache
        dockerfile_name = "dockerfile_" + str(uuid.uuid4())
        full_dockerfile_path = Path(self.layer_downloader.layer_cache, dockerfile_name)
        stream_writer = stream or StreamWriter(sys.stderr)

        try:
            with open(str(full_dockerfile_path), "w") as dockerfile:
                dockerfile.write(dockerfile_content)

            # add dockerfile and rapid source paths
            tar_paths = {str(full_dockerfile_path): "Dockerfile", self._RAPID_SOURCE_PATH: "/aws-lambda-rie"}

            for layer in layers:
                tar_paths[layer.codeuri] = "/" + layer.name

            # Set permission for all the files in the tarball to 500(Read and Execute Only)
            # This is need for systems without unix like permission bits(Windows) while creating a unix image
            # Without setting this explicitly, tar will default the permission to 666 which gives no execute permission
            def set_item_permission(tar_info):
                tar_info.mode = 0o500
                return tar_info

            # Set only on Windows, unix systems will preserve the host permission into the tarball
            tar_filter = set_item_permission if platform.system().lower() == "windows" else None

            with create_tarball(tar_paths, tar_filter=tar_filter) as tarballfile:
                try:
                    resp_stream = self.docker_client.api.build(
                        fileobj=tarballfile,
                        custom_context=True,
                        rm=True,
                        tag=docker_tag,
                        pull=not self.skip_pull_image,
                        decode=True,
                    )
                    for log in resp_stream:
                        stream_writer.write(".")
                        stream_writer.flush()
                        if "error" in log:
                            stream_writer.write("\n")
                            LOG.exception("Failed to build Docker Image")
                            raise ImageBuildException("Error building docker image: {}".format(log["error"]))
                    stream_writer.write("\n")
                except (docker.errors.BuildError, docker.errors.APIError) as ex:
                    stream_writer.write("\n")
                    LOG.exception("Failed to build Docker Image")
                    raise ImageBuildException("Building Image failed.") from ex
        finally:
            if full_dockerfile_path.exists():
                full_dockerfile_path.unlink()

    @staticmethod
    def _generate_dockerfile(base_image, layers):
        """
        Generate the Dockerfile contents

        A generated Dockerfile will look like the following:
        ```
        FROM amazon/aws-sam-cli-emulation-image-python3.6:latest

        ADD init /var/rapid

        ADD layer1 /opt
        ADD layer2 /opt
        ```

        Parameters
        ----------
        base_image str
            Base Image to use for the new image
        layers list(samcli.commands.local.lib.provider.Layer)
            List of Layers to be use to mount in the image

        Returns
        -------
        str
            String representing the Dockerfile contents for the image

        """
        dockerfile_content = (
            f"FROM {base_image}\nADD aws-lambda-rie /var/rapid\nRUN chmod +x /var/rapid/aws-lambda-rie\n"
        )

        for layer in layers:
            dockerfile_content = dockerfile_content + f"ADD {layer.name} {LambdaImage._LAYERS_DIR}\n"
        return dockerfile_content
