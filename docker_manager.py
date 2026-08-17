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
                internal=True,          # нет выхода в интернет
                attachable=False,
                options={
                    "com.docker.network.bridge.enable_icc":           "false",  # нет связи между контейнерами
                    "com.docker.network.bridge.enable_ip_masquerade": "false",
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
                    mem_limit="50m",
                    memswap_limit="50m",          # swap = 0 (memswap == mem_limit)
                    cpu_quota=25_000,
                    cpu_period=100_000,
                    pids_limit=30,                # не более 30 процессов

                    # ── файловая система ─────────────────────────────────────
                    read_only=True,               # корень только для чтения
                    volumes={vol: {"bind": "/app", "mode": "rw"}},
                    working_dir="/app",
                    tmpfs={
                        "/tmp": "size=16m,mode=1777",  # /tmp в оперативке, 16 МБ
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
                        docker.types.Ulimit(name="nofile", soft=256, hard=512),
                        docker.types.Ulimit(name="nproc",  soft=30,  hard=30),
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
                return True, "📦 ZIP успешно загружен и распакован в /app"
            stderr = (out[1] or b"").decode(errors="replace")[-400:]
            return False, f"Ошибка распаковки:\n<code>{stderr}</code>"

        except Exception as exc:
            logger.exception("upload_zip failed")
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
            code, out = c.exec_run(
                f"pip install {package} --no-cache-dir",
                demux=True,
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

    def exec_command(self, container_id: str, cmd: str):
        """Выполнить произвольную команду в контейнере (для будущих расширений)."""
        if not DOCKER_AVAILABLE:
            return True, f"[симуляция] $ {cmd}"
        c = self._get(container_id)
        if c is None:
            return False, "Контейнер не найден"
        try:
            code, out = c.exec_run(cmd, demux=True)
            stdout = (out[0] or b"").decode(errors="replace")[-800:]
            stderr = (out[1] or b"").decode(errors="replace")[-400:]
            text = (stdout + stderr).strip()
            return code == 0, text or "(нет вывода)"
        except Exception as exc:
            return False, str(exc)


docker_mgr = DockerManager()
