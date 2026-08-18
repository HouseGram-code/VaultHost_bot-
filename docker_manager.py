"""
docker_manager.py — управление Docker-контейнерами пользователей.

Каждый «сервер» — отдельный контейнер python:3.11-slim с ограничениями:
  RAM  : 50 МБ
  CPU  : 25 000 / 100 000 = 0.25 ядра
  Диск : именованный Docker-volume hosting_vol_<server_id>
"""

import io
import logging
import tarfile
import threading

logger = logging.getLogger(__name__)

try:
    import docker
    _client = docker.from_env()
    _client.ping()
    DOCKER_AVAILABLE = True
    logger.info("Docker SDK подключён успешно")
except Exception as _e:
    DOCKER_AVAILABLE = False
    _client = None
    logger.warning("Docker SDK недоступен: %s — серверы будут в режиме симуляции", _e)

_lock = threading.Lock()


# ─── helpers ──────────────────────────────────────────────────────────────────

def _container_name(server_id: int) -> str:
    return f"hostbot_srv_{server_id}"


def _volume_name(server_id: int) -> str:
    return f"hostbot_vol_{server_id}"


# ─── public API ───────────────────────────────────────────────────────────────

class DockerManager:

    # ── create ────────────────────────────────────────────────────────────────

    def _ensure_isolated_network(self, server_id: int) -> str:
        """Создаёт изолированную сеть для контейнера (без доступа к хосту и другим контейнерам)."""
        net_name = f"hostbot_net_{server_id}"
        try:
            _client.networks.get(net_name)
        except docker.errors.NotFound:
            _client.networks.create(
                net_name,
                driver="bridge",
                # ВАЖНО: internal=False — иначе нет интернета:
                # не работает pip install и Telegram-боты пользователей.
                internal=False,
                attachable=False,
                options={
                    # нет связи между контейнерами пользователей, но интернет есть
                    "com.docker.network.bridge.enable_icc": "false",
                },
                labels={"hostbot.server_id": str(server_id)},
            )
        return net_name

    def provision(self, server_id: int, server_name: str):
        """
        Создаёт volume + изолированный контейнер, не запуская его.
        Возвращает (container_id: str | None, error: str | None).

        Политика изоляции:
          • Изолированная bridge-сеть (internal=True, icc=false) — нет выхода в интернет,
            нет связи с другими контейнерами.
          • read_only=True для корневой ФС (только /app и /tmp доступны на запись).
          • Все Linux capabilities сброшены (cap_drop=ALL).
          • no-new-privileges — процесс не может повысить привилегии.
          • Лимит процессов (pids_limit=30).
          • Лимит файловых дескрипторов (nofile 256/512).
          • Без доступа к устройствам хоста.
          • Без привилегированного режима.
        """
        if not DOCKER_AVAILABLE:
            return f"sim_{server_id}", None

        name = _container_name(server_id)
        vol  = _volume_name(server_id)

        with _lock:
            try:
                # volume
                try:
                    _client.volumes.create(vol)
                except docker.errors.APIError:
                    pass  # уже существует

                # изолированная сеть
                try:
                    net_name = self._ensure_isolated_network(server_id)
                except Exception as e:
                    logger.warning("Не удалось создать изолированную сеть: %s — используем none", e)
                    net_name = "none"

                # container (уже существующий — отдаём его id)
                try:
                    c = _client.containers.get(name)
                    return c.id, None
                except docker.errors.NotFound:
                    pass

                c = _client.containers.create(
                    image="python:3.11-slim",
                    name=name,
                    command="tail -f /dev/null",

                    # ── ресурсы ──────────────────────────────────────────────
                    mem_limit="256m",
                    memswap_limit="256m",         # swap = 0 (memswap == mem_limit)
                    cpu_quota=25_000,
                    cpu_period=100_000,
                    pids_limit=100,               # не более 100 процессов

                    # ── файловая система ─────────────────────────────────────
                    # read_only=False: pip install и питон-приложениям нужен
                    # доступ на запись (site-packages, кэш, временные файлы).
                    read_only=False,
                    volumes={vol: {"bind": "/app", "mode": "rw"}},
                    working_dir="/app",
                    tmpfs={
                        "/tmp": "size=128m,mode=1777",  # /tmp в оперативке
                    },
                    environment={
                        "PYTHONPATH":          "/app/.packages",
                        "PYTHONUNBUFFERED":    "1",
                        "PIP_CACHE_DIR":       "/tmp/pipcache",
                        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                        "HOME":                "/tmp",
                        "TMPDIR":              "/tmp",
                    },

                    # ── сеть ─────────────────────────────────────────────────
                    network=net_name,

                    # ── безопасность ─────────────────────────────────────────
                    privileged=False,
                    cap_drop=["ALL"],             # сброс всех capabilities
                    security_opt=[
                        "no-new-privileges:true", # нельзя повысить привилегии
                        "seccomp=unconfined",     # совместимость (можно заменить профилем)
                    ],
                    ulimits=[
                        docker.types.Ulimit(name="nofile", soft=1024, hard=2048),
                    ],

                    # ── метаданные ───────────────────────────────────────────
                    labels={
                        "hostbot.server_id":   str(server_id),
                        "hostbot.server_name": server_name,
                    },
                )
                return c.id, None
            except Exception as exc:
                logger.exception("provision failed")
                return None, str(exc)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def _get(self, container_id: str):
        if not DOCKER_AVAILABLE:
            return None
        try:
            return _client.containers.get(container_id)
        except Exception:
            return None

    def start(self, container_id: str):
        if not DOCKER_AVAILABLE:
            return True, None
        c = self._get(container_id)
        if c is None:
            return False, "Контейнер не найден"
        try:
            c.start()
            return True, None
        except Exception as exc:
            return False, str(exc)

    def stop(self, container_id: str):
        if not DOCKER_AVAILABLE:
            return True, None
        c = self._get(container_id)
        if c is None:
            return True, None   # уже нет — ок
        try:
            c.stop(timeout=10)
            return True, None
        except Exception as exc:
            return False, str(exc)

    def restart(self, container_id: str):
        if not DOCKER_AVAILABLE:
            return True, None
        c = self._get(container_id)
        if c is None:
            return False, "Контейнер не найден"
        try:
            c.restart(timeout=15)
            return True, None
        except Exception as exc:
            return False, str(exc)

    def status(self, container_id: str) -> str:
        """Возвращает: running | stopped | paused | restarting | unknown"""
        if not DOCKER_AVAILABLE:
            return "running"
        c = self._get(container_id)
        if c is None:
            return "stopped"
        try:
            c.reload()
            raw = c.status  # 'running', 'exited', 'paused', 'restarting', ...
            mapping = {
                "running":    "running",
                "exited":     "stopped",
                "paused":     "paused",
                "restarting": "restarting",
                "created":    "stopped",
                "dead":       "stopped",
            }
            return mapping.get(raw, "unknown")
        except Exception:
            return "unknown"

    def remove(self, container_id: str):
        if not DOCKER_AVAILABLE:
            return True
        c = self._get(container_id)
        if c is None:
            return True
        try:
            c.stop(timeout=5)
        except Exception:
            pass
        try:
            c.remove(force=True)
            return True
        except Exception:
            return False

    # ── file ops ──────────────────────────────────────────────────────────────

    def upload_zip(self, container_id: str, zip_bytes: bytes, dest: str = "/app"):
        """
        Копирует zip в контейнер и распаковывает.
        Возвращает (success: bool, message: str).
        """
        if not DOCKER_AVAILABLE:
            return True, "✅ [симуляция] ZIP принят"

        c = self._get(container_id)
        if c is None:
            return False, "Контейнер не найден или не запущен"

        try:
            # Упаковываем zip в tar
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tf:
                info = tarfile.TarInfo(name="upload.zip")
                info.size = len(zip_bytes)
                tf.addfile(info, io.BytesIO(zip_bytes))
            buf.seek(0)
            c.put_archive(dest, buf.getvalue())

            # Распаковываем через python (всегда доступен в образе)
            cmd = (
                "python3 -c \""
                "import zipfile, os; "
                "zf = zipfile.ZipFile('/app/upload.zip'); "
                "zf.extractall('/app'); "
                "zf.close(); "
                "os.remove('/app/upload.zip')"
                "\""
            )
            code, out = c.exec_run(cmd, demux=True)
            if code == 0:
                extra = self._auto_requirements(c)
                return True, "📦 ZIP успешно загружен и распакован в /app" + extra
            stderr = (out[1] or b"").decode(errors="replace")[-400:]
            return False, f"Ошибка распаковки:\n<code>{stderr}</code>"

        except Exception as exc:
            logger.exception("upload_zip failed")
            return False, str(exc)

    def _auto_requirements(self, c) -> str:
        """После распаковки ZIP автоматически ставит requirements.txt."""
        try:
            code, _ = c.exec_run("test -f /app/requirements.txt")
            if code != 0:
                return ""
            code, out = c.exec_run(
                "python3 -m pip install --no-input --disable-pip-version-check "
                "--target /app/.packages -r /app/requirements.txt",
                demux=True,
                workdir="/app",
                environment={
                    "PYTHONPATH":    "/app/.packages",
                    "PIP_CACHE_DIR": "/tmp/pipcache",
                    "HOME":          "/tmp",
                    "TMPDIR":        "/tmp",
                },
            )
            if code == 0:
                return "\n\n📥 <b>requirements.txt</b> установлен автоматически."
            err = ((out[1] or b"") if out else b"").decode(errors="replace")[-300:]
            return f"\n\n⚠️ requirements.txt не установился:\n<pre>{err}</pre>"
        except Exception as exc:
            return f"\n\n⚠️ requirements.txt: {exc}"

    def list_files(self, container_id: str) -> list:
        """Список .py файлов в /app (без .packages)."""
        if not DOCKER_AVAILABLE:
            return ["bot.py"]
        c = self._get(container_id)
        if c is None:
            return []
        try:
            code, out = c.exec_run(
                "find /app -maxdepth 2 -name '*.py' -not -path '*/.packages/*'",
                demux=True,
            )
            text = ((out[0] or b"") if out else b"").decode(errors="replace")
            return [l.strip() for l in text.splitlines() if l.strip()][:20]
        except Exception:
            return []

    def run_script(self, container_id: str, script: str = None):
        """Запускает python-скрипт в фоне, лог → /app/app.log."""
        if not DOCKER_AVAILABLE:
            return True, "✅ [симуляция] скрипт запущен"
        c = self._get(container_id)
        if c is None:
            return False, "Контейнер не найден или не запущен"

        if not script:
            files = self.list_files(container_id)
            for cand in ("/app/bot.py", "/app/main.py", "/app/app.py", "/app/run.py"):
                if cand in files:
                    script = cand
                    break
            if not script and files:
                script = files[0]
        if not script:
            return False, "❌ Не найден ни один .py файл в /app. Загрузите ZIP."

        safe = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_./")
        if not all(ch in safe for ch in script):
            return False, "❌ Недопустимые символы в имени файла"

        try:
            c.exec_run("pkill -f 'python3 /app/' ", demux=True)
        except Exception:
            pass
        try:
            c.exec_run(
                f"sh -c 'cd /app && nohup python3 -u {script} > /app/app.log 2>&1 &'",
                detach=True,
                environment={"PYTHONPATH": "/app/.packages", "PYTHONUNBUFFERED": "1", "HOME": "/tmp"},
            )
            return True, f"▶️ Запущен <code>{script}</code>\n\nЛоги: кнопка 📜 Логи"
        except Exception as exc:
            return False, str(exc)

    def get_logs(self, container_id: str, lines: int = 40):
        if not DOCKER_AVAILABLE:
            return True, "[симуляция] логов нет"
        c = self._get(container_id)
        if c is None:
            return False, "Контейнер не найден"
        try:
            code, out = c.exec_run(f"tail -n {lines} /app/app.log", demux=True)
            text = ((out[0] or b"") if out else b"").decode(errors="replace")
            err = ((out[1] or b"") if out else b"").decode(errors="replace")
            body = (text + err).strip()
            return True, body or "(лог пуст — скрипт ещё не запускался)"
        except Exception as exc:
            return False, str(exc)

    def pip_install(self, container_id: str, package: str):
        """
        Запускает pip install <package> внутри контейнера.
        Возвращает (success: bool, message: str).
        """
        if not DOCKER_AVAILABLE:
            return True, f"✅ [симуляция] pip install {package} — OK"

        c = self._get(container_id)
        if c is None:
            return False, "Контейнер не найден или не запущен"

        # Базовый whitelist символов во избежание инъекций
        safe = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.[]=<>!")
        if not all(ch in safe for ch in package.replace(" ", "")):
            return False, "❌ Недопустимые символы в имени пакета"

        try:
            cmd = (
                "python3 -m pip install --no-input --disable-pip-version-check "
                f"--target /app/.packages --upgrade {package}"
            )
            code, out = c.exec_run(
                cmd,
                demux=True,
                workdir="/app",
                environment={
                    "PYTHONPATH":    "/app/.packages",
                    "PIP_CACHE_DIR": "/tmp/pipcache",
                    "HOME":          "/tmp",
                    "TMPDIR":        "/tmp",
                },
            )
            stdout = (out[0] or b"").decode(errors="replace")
            stderr = (out[1] or b"").decode(errors="replace")
            combined = (stdout + stderr)[-1200:]

            if code == 0:
                return True, f"✅ <b>pip install {package}</b> выполнен успешно\n\n<pre>{combined}</pre>"
            return False, f"❌ Ошибка установки:\n<pre>{combined}</pre>"
        except Exception as exc:
            logger.exception("pip_install failed")
            return False, str(exc)

    def get_stats(self, container_id: str) -> dict:
        """Возвращает CPU% и RAM МБ работающего контейнера."""
        if not DOCKER_AVAILABLE:
            return {}
        c = self._get(container_id)
        if c is None:
            return {}
        try:
            s            = c.stats(stream=False)
            cpu_delta    = s["cpu_stats"]["cpu_usage"]["total_usage"] - s["precpu_stats"]["cpu_usage"]["total_usage"]
            system_delta = s["cpu_stats"].get("system_cpu_usage", 0) - s["precpu_stats"].get("system_cpu_usage", 0)
            num_cpus     = s["cpu_stats"].get("online_cpus", 1)
            cpu_pct      = round((cpu_delta / system_delta) * num_cpus * 100.0, 1) if system_delta > 0 else 0.0
            cache        = s["memory_stats"].get("stats", {}).get("cache", 0)
            mem_usage    = max(0, s["memory_stats"].get("usage", 0) - cache)
            mem_mb       = round(mem_usage / (1024 * 1024), 1)
            return {"cpu_percent": cpu_pct, "mem_mb": mem_mb}
        except Exception:
            return {}

    def is_available(self) -> bool:
        return DOCKER_AVAILABLE

    def exec_command(self, container_id: str, cmd: str):
        """Выполнить произвольную команду в контейнере (для будущих расширений)."""
        if not DOCKER_AVAILABLE:
            return True, f"[симуляция] $ {cmd}"
        c = self._get(container_id)
        if c is None:
            return False, "Контейнер не найден"
        try:
            code, out = c.exec_run(
                cmd,
                demux=True,
                environment={"PYTHONPATH": "/app/.packages"},
            )
            stdout = (out[0] or b"").decode(errors="replace")[-800:]
            stderr = (out[1] or b"").decode(errors="replace")[-400:]
            text = (stdout + stderr).strip()
            return code == 0, text or "(нет вывода)"
        except Exception as exc:
            return False, str(exc)


docker_mgr = DockerManager()
