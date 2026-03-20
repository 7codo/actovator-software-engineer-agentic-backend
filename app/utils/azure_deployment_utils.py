"""
https://claude.ai/chat/a1883ad8-5369-4970-b53c-a1d024ebd22b
"""

import os
import time
import tarfile
import tempfile
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import docker
import git
from azure.identity import DefaultAzureCredential
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.containerregistry import ContainerRegistryManagementClient
from azure.mgmt.containerregistry.models import (
    Registry,
    Sku as AcrSku,
    RegistryUpdateParameters,
)
from azure.mgmt.appcontainers import ContainerAppsAPIClient
from azure.mgmt.appcontainers.models import (
    ManagedEnvironment,
    ContainerApp,
    Configuration,
    Template,
    Container,
    RegistryCredentials,
    Ingress,
    Scale,
    AppLogsConfiguration,
    LogAnalyticsConfiguration,
)


# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────


@dataclass
class DeploymentConfig:
    """All settings needed for a deployment."""

    # Azure identity
    subscription_id: str

    # Resource location
    resource_group: str
    location: str = "eastus"

    # Azure Container Registry
    acr_name: str = ""  # auto-generated if empty
    acr_sku: str = "Basic"  # Basic | Standard | Premium

    # Container App settings
    app_name: str = "my-container-app"
    environment_name: str = ""  # auto-generated if empty
    image_tag: str = "latest"

    # Ingress / networking
    target_port: int = 3000  # port your app listens on
    external_ingress: bool = True  # publicly reachable?

    # Scaling
    min_replicas: int = 0  # 0 = scale to zero
    max_replicas: int = 10

    # Environment variables passed to the container
    env_vars: dict = field(default_factory=dict)

    # CPU / memory
    cpu: float = 0.5  # vCPU
    memory: str = "1Gi"

    def __post_init__(self):
        if not self.acr_name:
            self.acr_name = f"{self.app_name.replace('-', '')}acr"
        if not self.environment_name:
            self.environment_name = f"{self.app_name}-env"


# ──────────────────────────────────────────────
# Logger helper
# ──────────────────────────────────────────────


class _Log:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    RESET = "\033[0m"
    BOLD = "\033[1m"

    @staticmethod
    def info(msg):
        print(f"  {_Log.CYAN}→{_Log.RESET} {msg}")

    @staticmethod
    def ok(msg):
        print(f"  {_Log.GREEN}✔{_Log.RESET} {msg}")

    @staticmethod
    def warn(msg):
        print(f"  {_Log.YELLOW}⚠{_Log.RESET} {msg}")

    @staticmethod
    def error(msg):
        print(f"  {_Log.RED}✘{_Log.RESET} {msg}")

    @staticmethod
    def step(msg):
        print(f"\n{_Log.BOLD}{_Log.CYAN}[{msg}]{_Log.RESET}")


# ──────────────────────────────────────────────
# Core Deployer
# ──────────────────────────────────────────────


class AzureContainerAppsDeployer:
    """
    Deploy a GitHub repository or a local directory to Azure Container Apps.

    Usage
    -----
    config = DeploymentConfig(subscription_id="...", resource_group="my-rg", app_name="my-app")
    deployer = AzureContainerAppsDeployer(config)

    # From GitHub
    deployer.deploy_from_github("https://github.com/user/repo", branch="main")

    # From local files
    deployer.deploy_from_local("/path/to/project")
    """

    def __init__(self, config: DeploymentConfig):
        self.config = config
        self.credential = DefaultAzureCredential()
        self._docker = docker.from_env()

        self._resource_client = ResourceManagementClient(
            self.credential, config.subscription_id
        )
        self._acr_client = ContainerRegistryManagementClient(
            self.credential, config.subscription_id
        )
        self._aca_client = ContainerAppsAPIClient(
            self.credential, config.subscription_id
        )

        self._acr_login_server: str = ""
        self._acr_username: str = ""
        self._acr_password: str = ""

    # ── Public entry points ──────────────────────

    def deploy_from_github(self, repo_url: str, branch: str = "main") -> str:
        """
        Clone a GitHub repository and deploy it to Azure Container Apps.

        Parameters
        ----------
        repo_url : str
            HTTPS URL of the GitHub repo, e.g. "https://github.com/user/repo"
        branch : str
            Branch to clone (default: "main")

        Returns
        -------
        str
            Public URL of the deployed Container App
        """
        print(f"\n{'=' * 55}")
        print(f"  🚀  Deploying from GitHub → Azure Container Apps")
        print(f"      Repo   : {repo_url}")
        print(f"      Branch : {branch}")
        print(f"{'=' * 55}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            _Log.step("1/5  Cloning repository")
            self._clone_repo(repo_url, branch, tmp_dir)
            return self._run_pipeline(tmp_dir)

    def deploy_from_local(self, local_path: str) -> str:
        """
        Deploy a local directory to Azure Container Apps.

        Parameters
        ----------
        local_path : str
            Absolute or relative path to the project directory.
            Must contain a Dockerfile (or one will be auto-generated for Node.js).

        Returns
        -------
        str
            Public URL of the deployed Container App
        """
        path = str(Path(local_path).resolve())
        print(f"\n{'=' * 55}")
        print(f"  🚀  Deploying Local Files → Azure Container Apps")
        print(f"      Path : {path}")
        print(f"{'=' * 55}")

        return self._run_pipeline(path)

    # ── Pipeline orchestration ───────────────────

    def _run_pipeline(self, source_dir: str) -> str:
        """Shared pipeline: ACR → build → push → deploy."""
        cfg = self.config

        _Log.step("2/5  Ensuring Azure infrastructure")
        self._ensure_resource_group()
        self._ensure_acr()

        _Log.step("3/5  Building & pushing Docker image")
        self._ensure_dockerfile(source_dir)
        image_ref = self._build_and_push_image(source_dir)

        _Log.step("4/5  Provisioning Container Apps environment")
        env_id = self._ensure_environment()

        _Log.step("5/5  Deploying Container App")
        app_url = self._deploy_container_app(image_ref, env_id)

        print(f"\n{'=' * 55}")
        _Log.ok(f"Deployment complete!")
        print(f"  🌐  URL : {app_url}")
        print(f"{'=' * 55}\n")
        return app_url

    # ── Step helpers ─────────────────────────────

    def _clone_repo(self, repo_url: str, branch: str, dest: str):
        """Clone the repository into dest."""
        _Log.info(f"Cloning {repo_url} (branch: {branch}) …")
        git.Repo.clone_from(repo_url, dest, branch=branch, depth=1)
        _Log.ok("Repository cloned.")

    def _ensure_resource_group(self):
        cfg = self.config
        _Log.info(f"Resource group: {cfg.resource_group}")
        self._resource_client.resource_groups.create_or_update(
            cfg.resource_group,
            {"location": cfg.location},
        )
        _Log.ok("Resource group ready.")

    def _ensure_acr(self):
        """Create ACR if it doesn't exist and retrieve credentials."""
        cfg = self.config
        _Log.info(f"Container registry: {cfg.acr_name}")

        # Create or update
        poller = self._acr_client.registries.begin_create(
            cfg.resource_group,
            cfg.acr_name,
            Registry(
                location=cfg.location,
                sku=AcrSku(name=cfg.acr_sku),
                admin_user_enabled=True,
            ),
        )
        registry = poller.result()
        self._acr_login_server = registry.login_server
        _Log.ok(f"Registry ready: {self._acr_login_server}")

        # Fetch admin credentials
        creds = self._acr_client.registries.list_credentials(
            cfg.resource_group, cfg.acr_name
        )
        self._acr_username = creds.username
        self._acr_password = creds.passwords[0].value

        # Docker login
        self._docker.login(
            username=self._acr_username,
            password=self._acr_password,
            registry=self._acr_login_server,
        )
        _Log.ok("Docker logged into ACR.")

    def _ensure_dockerfile(self, source_dir: str):
        """
        Auto-generate a production Dockerfile if one is missing.
        Detects Next.js / generic Node.js / Python projects.
        """
        dockerfile = Path(source_dir) / "Dockerfile"
        if dockerfile.exists():
            _Log.ok("Dockerfile found.")
            return

        _Log.warn("No Dockerfile found — auto-generating one.")

        # Detect project type
        if (Path(source_dir) / "next.config.js").exists() or (
            Path(source_dir) / "next.config.ts"
        ).exists():
            content = self._nextjs_dockerfile()
            _Log.info("Detected Next.js project.")
        elif (Path(source_dir) / "package.json").exists():
            content = self._nodejs_dockerfile()
            _Log.info("Detected Node.js project.")
        elif (Path(source_dir) / "requirements.txt").exists():
            content = self._python_dockerfile()
            _Log.info("Detected Python project.")
        else:
            raise RuntimeError(
                "Cannot auto-detect project type. "
                "Please add a Dockerfile to your project root."
            )

        dockerfile.write_text(content)
        _Log.ok("Dockerfile generated.")

    def _build_and_push_image(self, source_dir: str) -> str:
        """Build the Docker image and push it to ACR. Returns the full image ref."""
        cfg = self.config
        image_name = f"{self._acr_login_server}/{cfg.app_name}:{cfg.image_tag}"

        _Log.info(f"Building image: {image_name}")
        image, build_logs = self._docker.images.build(
            path=source_dir,
            tag=image_name,
            rm=True,
        )
        for chunk in build_logs:
            if "stream" in chunk:
                line = chunk["stream"].strip()
                if line:
                    print(f"    {line}")

        _Log.info("Pushing image to ACR …")
        for line in self._docker.images.push(image_name, stream=True, decode=True):
            status = line.get("status", "")
            progress = line.get("progress", "")
            if status and "Pushing" in status:
                print(f"    {status} {progress}", end="\r")
        print()
        _Log.ok("Image pushed successfully.")
        return image_name

    def _ensure_environment(self) -> str:
        """Create or retrieve a Container Apps Managed Environment. Returns its resource ID."""
        cfg = self.config
        _Log.info(f"Managed environment: {cfg.environment_name}")

        try:
            env = self._aca_client.managed_environments.get(
                cfg.resource_group, cfg.environment_name
            )
            _Log.ok("Existing environment found.")
            return env.id
        except Exception:
            pass  # doesn't exist yet

        poller = self._aca_client.managed_environments.begin_create_or_update(
            cfg.resource_group,
            cfg.environment_name,
            ManagedEnvironment(location=cfg.location),
        )
        env = poller.result()
        _Log.ok("Environment provisioned.")
        return env.id

    def _deploy_container_app(self, image_ref: str, env_id: str) -> str:
        """Create or update the Container App. Returns the public FQDN."""
        cfg = self.config

        # Build env-var list
        env_list = [{"name": k, "value": v} for k, v in cfg.env_vars.items()]

        container_app = ContainerApp(
            location=cfg.location,
            managed_environment_id=env_id,
            configuration=Configuration(
                registries=[
                    RegistryCredentials(
                        server=self._acr_login_server,
                        username=self._acr_username,
                        password_secret_ref="acr-password",
                    )
                ],
                secrets=[{"name": "acr-password", "value": self._acr_password}],
                ingress=Ingress(
                    external=cfg.external_ingress,
                    target_port=cfg.target_port,
                ),
            ),
            template=Template(
                containers=[
                    Container(
                        name=cfg.app_name,
                        image=image_ref,
                        resources={"cpu": cfg.cpu, "memory": cfg.memory},
                        env=env_list or None,
                    )
                ],
                scale=Scale(
                    min_replicas=cfg.min_replicas,
                    max_replicas=cfg.max_replicas,
                ),
            ),
        )

        _Log.info(f"Deploying container app: {cfg.app_name} …")
        poller = self._aca_client.container_apps.begin_create_or_update(
            cfg.resource_group,
            cfg.app_name,
            container_app,
        )
        app = poller.result()
        _Log.ok("Container App deployed.")

        fqdn = app.configuration.ingress.fqdn if app.configuration.ingress else "N/A"
        return f"https://{fqdn}"

    # ── Dockerfile templates ─────────────────────

    @staticmethod
    def _nextjs_dockerfile() -> str:
        return """\
FROM node:20-alpine AS deps
WORKDIR /app
COPY package*.json ./
RUN npm ci --only=production

FROM node:20-alpine AS builder
WORKDIR /app
COPY . .
COPY --from=deps /app/node_modules ./node_modules
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./package.json
COPY --from=builder /app/public ./public
EXPOSE 3000
CMD ["npm", "start"]
"""

    @staticmethod
    def _nodejs_dockerfile() -> str:
        return """\
FROM node:20-alpine
WORKDIR /app
COPY package*.json ./
RUN npm ci --only=production
COPY . .
EXPOSE 3000
CMD ["node", "index.js"]
"""

    @staticmethod
    def _python_dockerfile() -> str:
        return """\
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["python", "app.py"]
"""
