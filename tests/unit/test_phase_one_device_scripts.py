from pathlib import Path


def test_switch_scripts_only_elevate_the_fixed_nginx_reload() -> None:
    root = Path(__file__).parents[2] / "examples" / "phase1" / "device-scripts"

    for name in ("frontend-activate-release", "frontend-restore-release"):
        script = (root / name).read_text(encoding="utf-8")
        assert "/usr/bin/sudo -n /usr/bin/systemctl reload nginx" in script
        assert "sudo -n /usr/local/bin" not in script
        assert "NOPASSWD" not in script


def test_wsl_nginx_serves_the_declared_dist_artifact_root() -> None:
    root = Path(__file__).parents[2] / "examples" / "phase1" / "wsl"
    config = (root / "frontend-agent.nginx").read_text(encoding="utf-8")

    assert "root /srv/frontend-agent/current/dist;" in config


def test_user_mode_scripts_require_no_elevation_and_keep_release_boundary() -> None:
    root = (
        Path(__file__).parents[2]
        / "examples"
        / "phase1"
        / "user-mode-device-scripts"
    )
    scripts = {
        path.name: path.read_text(encoding="utf-8")
        for path in root.iterdir()
        if path.is_file()
    }

    assert set(scripts) == {
        "frontend-activate-release",
        "frontend-current-release",
        "frontend-health-check",
        "frontend-restore-release",
        "frontend-user-http-start",
    }
    assert all("sudo" not in script for script in scripts.values())
    assert all("systemctl" not in script for script in scripts.values())
    for name in (
        "frontend-activate-release",
        "frontend-current-release",
        "frontend-restore-release",
        "frontend-user-http-start",
    ):
        assert "${HOME}/frontend-agent" in scripts[name]

    for name in ("frontend-activate-release", "frontend-restore-release"):
        assert '"${release_root}"/*' in scripts[name]
        assert "outside the release root" in scripts[name]

    assert "http://127.0.0.1:8080/" in scripts["frontend-health-check"]
    server = scripts["frontend-user-http-start"]
    assert "python3 -m http.server 8080" in server
    assert "--bind 127.0.0.1" in server
    assert '"${runtime_root}/current/dist"' in server
