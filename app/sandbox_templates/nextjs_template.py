from e2b import Template, default_build_logger, wait_for_url
from app.constants import PROJECT_PATH
from app.core.config import settings


def install_python_313_cmd():
    return (
        "apt-get update "
        "&& apt-get install -y curl build-essential libssl-dev zlib1g-dev "
        "libbz2-dev libreadline-dev libsqlite3-dev libncursesw5-dev xz-utils "
        "tk-dev libxml2-dev libxmlsec1-dev libffi-dev liblzma-dev && "
        "curl -O https://www.python.org/ftp/python/3.13.0/Python-3.13.0.tgz && "
        "tar xzf Python-3.13.0.tgz && cd Python-3.13.0 && "
        "./configure --enable-optimizations --prefix=/usr/local && "
        "make -j$(nproc) && make altinstall && "
        "curl -sS https://bootstrap.pypa.io/get-pip.py | python3.13 && "
        "cd /home/user && rm -rf Python-3.13.0 Python-3.13.0.tgz"
    )


def install_nodejs_cmd():
    return (
        "curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && "
        "apt-get install -y nodejs"
    )


def install_github_cli_cmd():
    return (
        "curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && "
        'echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null && '
        "apt update && "
        "apt install gh -y"
    )


def install_global_tools_cmds():
    return [
        "npm install -g npm@latest",
        "pip install uv",
        "npm install -g pm2",
    ]


def install_lightpanda_and_agent_browser_cmds():
    return [
        "curl -L -o /usr/local/bin/lightpanda https://github.com/lightpanda-io/browser/releases/download/nightly/lightpanda-x86_64-linux && chmod a+x /usr/local/bin/lightpanda",
    ]


def init_actovator_cmd():
    return (
        "mkdir -p actovator && "
        'echo \'{"languages": ["bash", "markdown", "toml", "typescript", "yaml"]}\' > actovator/config.json'
    )


def install_playwright_and_agent_browser_cmds():
    return [
        "npm install -g agent-browser",
        "agent-browser install",
        "agent-browser install --with-deps",
    ]


def run_init_next_script_cmd():
    return [
        "sed -i 's/\\r//' actovator/init-next.sh",
        "chmod +x actovator/init-next.sh && actovator/init-next.sh",
        "rm actovator/init-next.sh",
    ]


def clone_serena_repo_cmd():
    # Shallow clone for faster CI image builds
    return "git clone --depth=1 https://github.com/7codo/serena.git /home/user/serena"


def install_lsp_servers_cmd():
    return [
        "npm install -g bash-language-server yaml-language-server",
        (
            "curl -L -o /usr/local/bin/marksman "
            "https://github.com/artempyanykh/marksman/releases/latest/download/marksman-linux-x64 "
            "&& chmod +x /usr/local/bin/marksman"
        ),
    ]


template = (
    Template()
    .from_ubuntu_image("22.04")
    .set_workdir("/home/user")
    .run_cmd(install_python_313_cmd(), user="root")
    .run_cmd(install_nodejs_cmd(), user="root")
    .run_cmd(install_github_cli_cmd(), user="root")
    .run_cmd(install_global_tools_cmds(), user="root")
    .run_cmd(install_lsp_servers_cmd(), user="root")
    .run_cmd(install_lightpanda_and_agent_browser_cmds(), user="root")
    .run_cmd(install_playwright_and_agent_browser_cmds(), user="root")
    .run_cmd(clone_serena_repo_cmd())
    .set_user("user")
    .set_workdir(PROJECT_PATH)
    .run_cmd(
        'npx create-next-app . --ts --tailwind --eslint --import-alias "@/*" '
        "--use-npm --app --no-react-compiler --src-dir --turbopack --yes"
    )
    .run_cmd(init_actovator_cmd())
    .copy("nextjs_cleanup_script.sh", "actovator/init-next.sh")
    .run_cmd(run_init_next_script_cmd())
    .copy("project_memory_template.md", "actovator/project_memory.md")
    .copy("system_memory_template.md", "actovator/system_memory.md")
    .set_start_cmd(
        f'pm2 start npm --name "project" -- run dev ; '
        f'pm2 start uv --name "serena" -- run --directory /home/user/serena serena-server --project {PROJECT_PATH}',
        wait_for_url("http://localhost:3000"),
    )
)

Template.build(
    template,
    alias="nextjs-latest",
    cpu_count=4,
    memory_mb=4096,
    on_build_logs=default_build_logger(),
    api_key=settings.e2b_api_key,
    # skip_cache=True,
)
