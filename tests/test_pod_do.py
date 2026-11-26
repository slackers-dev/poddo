import json
import subprocess
from pathlib import Path

import pytest

from pod_do import cli
from pod_do.api import ApiError, RunPod, ssh_of
from pod_do.config import TEMPLATE, ConfigError, GlobalConfig, Project
from pod_do.pod import Pods, env_hash, gpu_candidates, pod_name
from pod_do.task import fmt, resolve_args

GPUS = {
    "gpus": [
        {
            "id": "NVIDIA RTX A4500",
            "name": "RTX A4500",
            "memory": 20,
            "manufacturer": "NVIDIA",
            "price": {"secure": 0.25, "community": 0.19},
            "availability": "LOW",
        },
        {
            "id": "NVIDIA RTX A4000",
            "name": "RTX A4000",
            "memory": 16,
            "manufacturer": "NVIDIA",
            "price": {"secure": 0.25, "community": 0.17},
            "availability": "NONE",
        },
        {
            "id": "NVIDIA A40",
            "name": "A40",
            "memory": 48,
            "manufacturer": "NVIDIA",
            "price": {"secure": 0.59, "community": 0.4},
            "availability": "HIGH",
        },
    ]
}


class FakeApi:
    """Nimmt Anfragen auf und antwortet nach Regeln (methode, pfad) -> Liste von (status, body)."""

    def __init__(self, rules):
        self.rules = {k: list(v) for k, v in rules.items()}
        self.calls = []

    def __call__(self, method, url, headers, body):
        path = url.split("api.runpod.io", 1)[1].split("?", 1)[0]
        self.calls.append((method, path, json.loads(body) if body else None, headers))
        answers = self.rules.get((method, path))
        if not answers:
            return 404, json.dumps({"title": "Not Found", "status": 404, "detail": path}).encode()
        status, payload = answers.pop(0) if len(answers) > 1 else answers[0]
        return status, json.dumps(payload).encode()


def project(tmp_path: Path, text: str = TEMPLATE) -> Project:
    (tmp_path / "pod-do.toml").write_text(text)
    return Project.find(tmp_path)


def test_template_parses(tmp_path):
    p = project(tmp_path)
    assert p.name == tmp_path.name
    assert p.envs["main"].min_vram == 8
    assert "uv venv" in p.envs["main"].setup
    t = p.tasks["hello"]
    assert t.env == "main" and t.args == ["input", "name?"]
    assert t.download == {"out": "results/{name}"}


def test_project_errors(tmp_path):
    with pytest.raises(ConfigError, match="image"):
        project(tmp_path, "[env.x]\nsetup='a'\n")
    with pytest.raises(ConfigError, match="run"):
        project(tmp_path, "[env.x]\nimage='i'\n[task.t]\nenv='x'\n")
    with pytest.raises(ConfigError, match="setup_file"):
        project(tmp_path, "[env.x]\nimage='i'\nsetup_file='fehlt.sh'\n")


def test_setup_file_and_single_env_default(tmp_path):
    (tmp_path / "s.sh").write_text("echo setup\n")
    p = project(tmp_path, "[env.x]\nimage='i'\nsetup_file='s.sh'\n[task.t]\nrun='echo'\n")
    assert p.envs["x"].setup == "echo setup\n"
    assert p.tasks["t"].env == "x"


def test_resolve_args(tmp_path):
    p = project(tmp_path)
    f = tmp_path / "bild.jpg"
    f.write_text("x")
    v = resolve_args(p.tasks["hello"], [str(f)], tmp_path)
    assert v == {"input": str(f.resolve()), "name": "bild"}
    v = resolve_args(p.tasks["hello"], [str(f), "anders"], tmp_path)
    assert v["name"] == "anders"
    with pytest.raises(ConfigError, match="<input>"):
        resolve_args(p.tasks["hello"], [], tmp_path)
    assert fmt("results/{name}", v) == "results/anders"
    with pytest.raises(ConfigError):
        fmt("{gibtsnicht}", v)


def test_pod_name_and_hash(tmp_path):
    assert pod_name("Mein Projekt", "main") == "pod-do-mein-projekt-main"
    p = project(tmp_path)
    h = env_hash(p.envs["main"])
    p.envs["main"].boot = "anders"  # boot zaehlt nicht
    assert env_hash(p.envs["main"]) == h
    p.envs["main"].setup += "\necho neu"
    assert env_hash(p.envs["main"]) != h


def test_api_auth_and_errors():
    fake = FakeApi(
        {
            ("GET", "/v2/billing/balance"): [(200, {"balanceAmount": 21.5, "billingMode": "PREPAID"})],
            ("DELETE", "/v2/pods/x"): [(403, {"title": "Forbidden", "status": 403, "detail": "nope"})],
        }
    )
    api = RunPod("geheim", transport=fake)
    assert api.balance()["balanceAmount"] == 21.5
    assert fake.calls[0][3]["Authorization"] == "Bearer geheim"
    assert "geheim" not in fake.calls[0][1]  # Schluessel nie in der URL
    with pytest.raises(ApiError) as e:
        api.terminate_pod("x")
    assert e.value.status == 403


def test_pagination():
    fake = FakeApi(
        {
            ("GET", "/v2/pods"): [
                (200, {"pods": [{"id": "a"}], "pagination": {"hasNextPage": True, "nextCursor": "c"}}),
                (200, {"pods": [{"id": "b"}], "pagination": {"hasNextPage": False, "nextCursor": None}}),
            ]
        }
    )
    assert [p["id"] for p in RunPod("k", transport=fake).list_pods()] == ["a", "b"]


def test_gpu_candidates_order_and_filters():
    api = RunPod("k", transport=FakeApi({("GET", "/v2/catalog/gpus"): [(200, GPUS)]}))
    c = gpu_candidates(api, ["NVIDIA RTX A4000", "NVIDIA RTX A4500", "NVIDIA A40"], 8, "SECURE")
    assert [x.gpu for x in c] == ["NVIDIA RTX A4500", "NVIDIA A40"]  # A4000 ausverkauft
    c = gpu_candidates(api, ["NVIDIA RTX A4500", "A40"], 24, "COMMUNITY")
    assert [(x.gpu, x.price) for x in c] == [("NVIDIA A40", 0.4)]  # A4500 zu klein, Name statt ID geht


def test_create_falls_back_to_next_gpu(tmp_path):
    key = tmp_path / "id_test"
    key.write_text("privat")
    (tmp_path / "id_test.pub").write_text("ssh-ed25519 AAAA test")
    pod = {"id": "p1", "name": "x", "status": "PROVISIONING", "cost": 0.59}
    fake = FakeApi(
        {
            ("GET", "/v2/catalog/gpus"): [(200, GPUS)],
            ("GET", "/v2/global-volumes"): [
                (
                    200,
                    {
                        "globalVolumes": [{"id": "gv1", "name": "pod-do"}],
                        "pagination": {"hasNextPage": False},
                    },
                )
            ],
            ("POST", "/v2/pods"): [
                (400, {"title": "Bad Request", "status": 400, "detail": "no capacity"}),
                (201, pod),
            ],
        }
    )
    cfg = GlobalConfig(ssh_key=key, gpus=["NVIDIA RTX A4500", "NVIDIA A40"])
    p = project(tmp_path)
    got = Pods(RunPod("k", transport=fake), cfg, p).create(p.envs["main"])
    assert got["id"] == "p1"
    posts = [c[2] for c in fake.calls if c[0] == "POST"]
    assert [b["gpu"]["id"] for b in posts] == ["NVIDIA RTX A4500", "NVIDIA A40"]
    b = posts[-1]
    assert b["mounts"] == {"global": [{"volumeId": "gv1", "path": "/vol"}]}
    assert b["env"]["PUBLIC_KEY"] == "ssh-ed25519 AAAA test"
    assert b["ports"] == ["22/tcp"] and b["name"] == pod_name(p.name, "main")


def test_create_stops_on_balance(tmp_path):
    key = tmp_path / "k"
    key.write_text("")
    (tmp_path / "k.pub").write_text("ssh-ed25519 A")
    fake = FakeApi(
        {
            ("GET", "/v2/catalog/gpus"): [(200, GPUS)],
            ("GET", "/v2/global-volumes"): [(200, {"globalVolumes": [], "pagination": {}})],
            ("POST", "/v2/pods"): [(402, {"title": "Payment Required", "status": 402, "detail": "balance"})],
        }
    )
    p = project(tmp_path)
    with pytest.raises(ApiError) as e:
        Pods(RunPod("k", transport=fake), GlobalConfig(ssh_key=key, gpus=["NVIDIA A40"]), p).create(
            p.envs["main"]
        )
    assert e.value.status == 402
    assert sum(1 for c in fake.calls if c[0] == "POST") == 1


def test_ssh_of():
    assert ssh_of({"ssh": {"direct": None}}) is None
    s = ssh_of({"ssh": {"direct": {"host": "1.2.3.4", "port": 40000, "username": "root", "command": ""}}})
    assert (s.host, s.port, s.user) == ("1.2.3.4", 40000, "root")


def test_cli_help_and_init(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init"]) == 0
    assert (tmp_path / "pod-do.toml").exists()
    assert cli.main(["init"]) == 1


def test_pod_scripts_are_valid_bash():
    from pod_do import pod_scripts as S

    for name in ("BOOTSTRAP_SH", "START_SH", "FOLLOW_SH", "WATCHDOG_SH", "START_WATCHDOG", "ENV_SH"):
        r = subprocess.run(["bash", "-n"], input=getattr(S, name), text=True, capture_output=True)
        assert r.returncode == 0, f"{name}: {r.stderr}"


def test_dotenv_key(tmp_path, monkeypatch):
    from pod_do.config import api_key, dotenv_key

    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert dotenv_key(sub) == ""
    (tmp_path / ".env").write_text("# kommentar\nOTHER=1\nexport POD_API_KEY='abc123'\n")
    assert dotenv_key(sub) == "abc123"
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.delenv("POD_API_KEY", raising=False)
    monkeypatch.chdir(sub)
    assert api_key() == "abc123"
    monkeypatch.setenv("RUNPOD_API_KEY", "vonenv")
    assert api_key() == "vonenv"


def test_create_waits_for_gpu(tmp_path, monkeypatch):
    import pod_do.pod as podmod

    key = tmp_path / "k"
    key.write_text("")
    (tmp_path / "k.pub").write_text("ssh-ed25519 A")
    sold_out = {"gpus": [dict(GPUS["gpus"][2], availability="LOW")]}
    fake = FakeApi(
        {
            ("GET", "/v2/catalog/gpus"): [(200, sold_out)],
            ("GET", "/v2/global-volumes"): [(200, {"globalVolumes": [], "pagination": {}})],
            ("POST", "/v2/pods"): [
                (400, {"title": "Bad Request", "status": 400, "detail": "weg"}),
                (201, {"id": "p2", "name": "x", "status": "PROVISIONING"}),
            ],
        }
    )
    sleeps = []
    monkeypatch.setattr(podmod.time, "sleep", lambda s: sleeps.append(s))
    p = project(tmp_path)
    cfg = GlobalConfig(ssh_key=key, gpus=["NVIDIA A40"])
    pods = Pods(RunPod("k", transport=fake), cfg, p)
    with pytest.raises(ConfigError, match="--wait"):
        Pods(
            RunPod(
                "k",
                transport=FakeApi(
                    {
                        **fake.rules,
                        ("POST", "/v2/pods"): [(400, {"title": "", "status": 400, "detail": "weg"})],
                    }
                ),
            ),
            cfg,
            p,
        ).create(p.envs["main"])
    assert pods.create(p.envs["main"], wait_minutes=5)["id"] == "p2"
    assert sleeps == [30]
