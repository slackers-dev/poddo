import json
import subprocess
from pathlib import Path

import pytest

from poddo import cli
from poddo.api import ApiError, RunPod, ssh_of
from poddo.config import TEMPLATE, ConfigError, GlobalConfig, Project
from poddo.pod import Pods, env_hash, gpu_candidates, pod_name
from poddo.task import fmt, resolve_args

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
    (tmp_path / "poddo.toml").write_text(text)
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
    assert pod_name("Mein Projekt", "main") == "poddo-mein-projekt-main"
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
                        "globalVolumes": [{"id": "gv1", "name": "poddo"}],
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
    assert (tmp_path / "poddo.toml").exists()
    assert cli.main(["init"]) == 1


def test_pod_scripts_are_valid_bash():
    from poddo import pod_scripts as S

    names = ("BOOTSTRAP_SH", "START_SH", "FOLLOW_SH", "WATCHDOG_SH", "START_WATCHDOG", "ENV_SH")
    for name in names + (
        "STATUS_SH",
        "PROGRESS_SH",
        "PUSH_SH",
        "SAVE_SH",
        "RESTORE_RESULTS_SH",
        "LEGACY_LINK",
    ):
        r = subprocess.run(["bash", "-n"], input=getattr(S, name), text=True, capture_output=True)
        assert r.returncode == 0, f"{name}: {r.stderr}"


def test_dotenv_key(tmp_path, monkeypatch):
    from poddo.config import api_key, dotenv_key

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
    import poddo.pod as podmod

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


def test_image_ref_parse():
    from poddo.registry import ImageRef

    r = ImageRef.parse("ghcr.io/slackers-dev/splattwin-lhm:91ea1d25aa96")
    assert (r.host, r.repo, r.tag) == ("ghcr.io", "slackers-dev/splattwin-lhm", "91ea1d25aa96")
    r = ImageRef.parse("runpod/pytorch:2.2.0-py3.10")
    assert (r.host, r.repo, r.tag) == ("registry-1.docker.io", "runpod/pytorch", "2.2.0-py3.10")
    assert ImageRef.parse("ubuntu").repo == "library/ubuntu"
    assert ImageRef.parse("localhost:5000/x/y:t").host == "localhost:5000"


def test_image_exists_uses_bearer_token(monkeypatch):
    from poddo import registry

    calls = []

    def fake_get(url, headers, method="GET"):
        calls.append((method, url, headers.get("Authorization", "")))
        if "token" in url:
            return 200, b'{"token": "abc"}', {}
        if headers.get("Authorization") == "Bearer abc":
            return (200 if url.endswith(":t1") or url.endswith("/t1") else 404), b"", {}
        return 401, b"", {"WWW-Authenticate": 'Bearer realm="https://ghcr.io/token",service="ghcr.io"'}

    monkeypatch.setattr(registry, "_get", fake_get)
    ref = registry.ImageRef.parse("ghcr.io/org/img:t1")
    assert registry.image_exists(ref, "u", "secret")
    assert any(c[2].startswith("Basic ") for c in calls)  # Token-Abfrage mit Benutzer und Token
    assert not registry.image_exists(registry.ImageRef.parse("ghcr.io/org/img:t2"), "u", "secret")


def test_create_with_image_and_registry(tmp_path):
    from poddo.pod import image_ref

    key = tmp_path / "k"
    key.write_text("")
    (tmp_path / "k.pub").write_text("ssh-ed25519 A")
    fake = FakeApi(
        {
            ("GET", "/v2/catalog/gpus"): [(200, GPUS)],
            ("GET", "/v2/global-volumes"): [(200, {"globalVolumes": [], "pagination": {}})],
            ("POST", "/v2/pods"): [(201, {"id": "p3", "name": "x", "status": "PROVISIONING"})],
        }
    )
    p = project(tmp_path, TEMPLATE.replace("min_vram = 8", 'min_vram = 8\ndocker = "ghcr.io/org/img"'))
    env = p.envs["main"]
    target = image_ref(env)
    assert target == f"ghcr.io/org/img:{env_hash(env)}"
    Pods(RunPod("k", transport=fake), GlobalConfig(ssh_key=key, gpus=["NVIDIA A40"]), p).create(
        env, image=target, registry_id="reg1"
    )
    body = [c[2] for c in fake.calls if c[0] == "POST"][0]
    assert body["image"] == target and body["registry"] == "reg1"


def test_post_not_retried_on_server_error(monkeypatch):
    import poddo.api as apimod

    monkeypatch.setattr(apimod.time, "sleep", lambda s: None)
    fake = FakeApi(
        {
            ("POST", "/v2/pods"): [(500, {"title": "Internal", "status": 500, "detail": "oops"})],
            ("GET", "/v2/pods/x"): [(502, {}), (200, {"id": "x"})],
        }
    )
    api = RunPod("k", transport=fake)
    with pytest.raises(ApiError) as e:
        api.create_pod({"name": "n"})
    assert e.value.status == 500
    assert sum(1 for c in fake.calls if c[0] == "POST") == 1  # sonst evtl. zwei Pods
    assert api.get_pod("x")["id"] == "x"  # GET darf man wiederholen


def test_network_error_becomes_api_error(monkeypatch):
    import poddo.api as apimod

    monkeypatch.setattr(apimod.time, "sleep", lambda s: None)
    calls = []

    def down(method, url, headers, body):
        calls.append(method)
        return 0, b'{"title": "Netzwerk", "detail": "timed out"}'

    api = RunPod("k", transport=down)
    with pytest.raises(ApiError, match="timed out"):
        api.balance()
    assert len(calls) == 5
    calls.clear()
    with pytest.raises(ApiError):
        api.create_pod({})
    assert len(calls) == 1


def test_ensure_deletes_fresh_pod_without_watchdog(tmp_path, monkeypatch):
    key = tmp_path / "k"
    key.write_text("")
    (tmp_path / "k.pub").write_text("ssh-ed25519 A")
    fake = FakeApi(
        {
            ("GET", "/v2/pods"): [(200, {"pods": [], "pagination": {}})],
            ("GET", "/v2/catalog/gpus"): [(200, GPUS)],
            ("GET", "/v2/global-volumes"): [(200, {"globalVolumes": [], "pagination": {}})],
            ("POST", "/v2/pods"): [(201, {"id": "p9", "name": "x", "status": "PROVISIONING"})],
            ("DELETE", "/v2/pods/p9"): [(204, {})],
        }
    )
    p = project(tmp_path)
    pods = Pods(RunPod("k", transport=fake), GlobalConfig(ssh_key=key, gpus=["NVIDIA A40"]), p)

    def no_ssh(pod, timeout=900):
        raise ConfigError("kein SSH")

    monkeypatch.setattr(pods, "wait_ssh", no_ssh)
    with pytest.raises(ConfigError, match="kein SSH"):
        pods.ensure("main")
    assert ("DELETE", "/v2/pods/p9") in [(c[0], c[1]) for c in fake.calls]


def test_ensure_keeps_existing_pod_on_error(tmp_path, monkeypatch):
    running = {"id": "old", "name": "", "status": "RUNNING"}
    p = project(tmp_path)
    running["name"] = pod_name(p.name, "main")
    fake = FakeApi({("GET", "/v2/pods"): [(200, {"pods": [running], "pagination": {}})]})
    pods = Pods(RunPod("k", transport=fake), GlobalConfig(ssh_key=None), p)
    monkeypatch.setattr(pods, "wait_ssh", lambda pod: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        pods.ensure("main")
    assert not any(c[0] == "DELETE" for c in fake.calls)  # fremder/laufender Pod bleibt


def test_unsafe_names_rejected(tmp_path):
    with pytest.raises(ConfigError, match="erlaubt"):
        project(tmp_path, "[env.'a b']\nimage='i'\n")
    with pytest.raises(ConfigError, match="erlaubt"):
        project(tmp_path, "[env.x]\nimage='i'\n[task.'$(rm)']\nrun='echo'\n")


def test_put_files_sends_one_tar(monkeypatch, tmp_path):
    import io
    import tarfile

    from poddo import remote as remmod
    from poddo.api import Ssh

    sent = []

    def fake_run(cmd, input=None, **kw):
        sent.append((cmd, input))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(remmod.subprocess, "run", fake_run)
    # zu langer Pfad fuer einen Unix-Socket: dann ohne gemeinsame Verbindung
    assert remmod.Remote(Ssh("h", 22, "root", "pod1"), None, state=Path("/x" * 60))._mux is None
    tmp_path = Path("/tmp/pd-test")
    r = remmod.Remote(Ssh("h", 22, "root", "pod1"), None, state=tmp_path)
    r._master_tried = True  # ohne Hintergrundverbindung
    r.put_files({"/opt/poddo/a.sh": ("echo a\n", 0o755), "/opt/poddo/b": ("b", 0o644)}, then="echo ok")
    assert len(sent) == 1
    cmd, data = sent[0]
    assert "HostKeyAlias=poddo-pod1" in cmd and f"ControlPath={tmp_path}/ssh/pod1" in cmd
    assert cmd[-1].endswith("&& echo ok'")
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        m = {i.name: i for i in t.getmembers()}
        assert m["opt/poddo/a.sh"].mode == 0o755
        assert t.extractfile("opt/poddo/b").read() == b"b"


def test_set_config_escapes(tmp_path, monkeypatch):
    import tomllib

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cli._set_config("registry_user", 'a"b\\c')
    cli._set_config("registry_user", "neu")
    data = tomllib.loads((tmp_path / "poddo" / "config.toml").read_text())
    assert data == {"registry_user": "neu"}
    cli._set_config("registry_user", 'x"y')
    assert tomllib.loads((tmp_path / "poddo" / "config.toml").read_text())["registry_user"] == 'x"y'


def test_parse_status_and_progress():
    from poddo.task import JobStatus, bar, describe, parse_status, progress_of

    out = (
        "a-1\trunning\t\t1000\t\t0\t3 12 Bilder\tsome log\n"
        "b-2\tdone\t0\t1000\t1130\t0\t\t 45%|####      | 45/100 [00:10<00:12]\n"
        "c-3\tdone\t1\t1000\t1100\t1\t\t\t0\n"
        "d-4\tdone\t0\t1000\t1100\t0\t\t\t1\n"
        "@now 1200\n"
    )
    st = parse_status(out)
    assert st["a-1"].state == "running" and st["a-1"].now == 1200
    assert progress_of(st["a-1"]) == (0.25, "3/12 Bilder")
    assert progress_of(st["b-2"]) == (0.45, "")
    assert progress_of(JobStatus("x", "running", last="kein fortschritt")) == (None, "")
    assert bar(0.5, 10) == "[#####-----]  50%"
    line = describe(st["a-1"], 60)
    assert "laeuft 3:20" in line and "25%" in line and "3/12 Bilder" in line
    assert "NICHT abgeholt" in describe(st["b-2"], 60) and "etwa 59 min" in describe(st["b-2"], 60)
    assert describe(st["c-3"], 60).endswith("abgeholt") and "Code 1" in describe(st["c-3"], 60)
    assert "verloren" in describe(JobStatus("d", "gone"), 60)
    assert st["d-4"].saved and "auf dem Volume gesichert" in describe(st["d-4"], 60)
    assert "nur auf dem Pod" in describe(st["b-2"], 60)
    assert "auf dem Volume" in describe(JobStatus("d", "gone"), 60, volume=True)
    assert "verloren" in describe(JobStatus("d", "lost"), 60, volume=True)
    assert describe(JobStatus("d", "gone", fetched=True), 60, volume=True) == "abgeholt"


def test_no_save_without_volume(tmp_path):
    sent = {}

    class R:
        def put_files(self, files, then=""):
            sent.update(files)

    Pods(None, GlobalConfig(ssh_key=None, volume=""), project(tmp_path)).start_job(
        R(), "j", "echo", save=["out"]
    )
    assert list(sent) == ["/opt/poddo/jobs/j/run.sh"]  # ohne Volume: nichts zu sichern


def test_save_config_written(tmp_path):
    from poddo.pod import results_archive

    sent = {}

    class R:
        def put_files(self, files, then=""):
            sent.update(files)

    p = project(tmp_path, TEMPLATE.replace("[env.main]", 'project = "Mein Projekt"\n[env.main]'))
    Pods(None, GlobalConfig(ssh_key=None, keep_results_days=7), p).start_job(
        R(), "j-1", "echo", save=["out", "extra/file.txt"]
    )
    conf = sent["/opt/poddo/jobs/j-1/save.conf"][0]
    assert "DEST=/vol/poddo/results/Mein-Projekt/j-1.tar.zst" in conf and "DAYS=7" in conf
    assert sent["/opt/poddo/jobs/j-1/save.list"][0] == "out\nextra/file.txt\n"
    assert results_archive("a/b c", "j") == "/vol/poddo/results/a-b-c/j.tar.zst"


# -- Umbenennung pod-do -> poddo: alte Pods, Volumes, Projekte und Ordner gehen weiter -----------------


def test_legacy_pod_volume_and_project(tmp_path):
    (tmp_path / "pod-do.toml").write_text(TEMPLATE)
    p = Project.find(tmp_path)
    old = {"id": "old", "name": pod_name(p.name, "main", "pod-do"), "status": "RUNNING"}
    fake = FakeApi(
        {
            ("GET", "/v2/pods"): [(200, {"pods": [old], "pagination": {}})],
            ("GET", "/v2/global-volumes"): [(200, {"globalVolumes": [{"id": "v1", "name": "pod-do"}]})],
        }
    )
    pods = Pods(RunPod("k", transport=fake), GlobalConfig(ssh_key=None, volume="poddo"), p)
    assert pods.find("main")["id"] == "old"
    assert [x["id"] for x in pods.mine()] == ["old"]
    assert pods.volume_id() == "v1"
    assert cli.is_ours("pod-do-x-main") and cli.is_ours("poddo-x-main") and not cli.is_ours("other")


def test_legacy_state_dir_renamed(tmp_path, monkeypatch):
    from poddo.config import state_dir

    (tmp_path / "pod-do" / "proj").mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert state_dir() == tmp_path / "poddo"
    assert (tmp_path / "poddo" / "proj").is_dir() and not (tmp_path / "pod-do").exists()


@pytest.mark.parametrize("old_real", [True, False])
def test_legacy_link_on_pod(tmp_path, old_real):
    from poddo import pod_scripts as S

    if old_real:  # Docker-Image oder alter Pod: echter Ordner /opt/pod-do mit Umgebung
        (tmp_path / "pod-do" / "envs").mkdir(parents=True)
    script = S.LEGACY_LINK.replace("/opt/", f"{tmp_path}/")
    for _ in range(2):  # zweimal: darf nichts kaputt machen
        subprocess.run(["bash", "-c", script], check=True)
    assert (tmp_path / "poddo").is_dir() and (tmp_path / "pod-do").is_dir()
    assert (tmp_path / ("poddo" if old_real else "pod-do")).is_symlink()
    (tmp_path / "poddo" / "x").write_text("1")
    assert (tmp_path / "pod-do" / "x").read_text() == "1"
