"""HTTP-клиент к сервису Audio Pipeline (отправка аудио, опрос статуса, получение результата)."""

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict

import httpx

from .config import AUDIO_PIPELINE_URL

logger = logging.getLogger(__name__)


class AudioPipelineClient:
    """Отправка jobs, poll статуса, получение транскрипции и диаризации."""

    def __init__(self):
        self.ready = False
        self.pipeline_url = AUDIO_PIPELINE_URL
        self.client = None

    def is_ready(self) -> bool:
        return self.ready

    def initialize(self):
        try:
            self.client = httpx.AsyncClient(
                timeout=3600.0,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
            try:
                with httpx.Client(timeout=10.0) as sync_client:
                    response = sync_client.get(f"{self.pipeline_url}/health")
                    if response.status_code == 200:
                        health = response.json()
                        self.ready = health.get("status") == "ready"
                        logger.info("Audio Pipeline: Ready" if self.ready else "Audio Pipeline: Not ready")
                    else:
                        logger.error("Audio Pipeline Error: %s", response.status_code)
                        self.ready = False
            except Exception as e:
                logger.error("Не удалось подключиться к Audio Pipeline: %s", e)
                logger.error("URL: %s", self.pipeline_url)
                self.ready = False
        except Exception as e:
            logger.error("Ошибка инициализации Audio Pipeline клиента: %s", e)
            self.ready = False

    async def submit_job(self, audio_path: str, priority: str = "high") -> str:
        if not self.ready or self.client is None:
            raise RuntimeError("Audio Pipeline сервис не готов")
        with open(audio_path, "rb") as f:
            files = {"audio": (Path(audio_path).name, f, "audio/wav")}
            data = {"priority": priority}
            response = await self.client.post(f"{self.pipeline_url}/jobs", files=files, data=data)
        if response.status_code != 200:
            raise RuntimeError(f"Audio Pipeline вернул ошибку: {response.status_code} - {response.text}")
        job_id = response.json().get("job_id")
        logger.info("Audio Pipeline: задача отправлена (job_id=%s)", job_id)
        return job_id

    async def get_status(self, job_id: str) -> Dict[str, Any]:
        if not self.ready or self.client is None:
            raise RuntimeError("Audio Pipeline сервис не готов")
        response = await self.client.get(f"{self.pipeline_url}/jobs/{job_id}/status")
        if response.status_code != 200:
            raise RuntimeError(f"Audio Pipeline вернул ошибку: {response.status_code} - {response.text}")
        return response.json()

    async def get_result(self, job_id: str) -> Dict[str, Any]:
        if not self.ready or self.client is None:
            raise RuntimeError("Audio Pipeline сервис не готов")
        response = await self.client.get(f"{self.pipeline_url}/jobs/{job_id}/result")
        if response.status_code != 200:
            raise RuntimeError(f"Audio Pipeline вернул ошибку: {response.status_code} - {response.text}")
        return response.json()

    async def wait_for_job(
        self,
        job_id: str,
        status_callback=None,
        poll_interval: float = 2.0,
    ) -> Dict[str, Any]:
        last_stage = None
        last_progress = None
        last_message = None
        queue_position = None
        
        while True:
            status = await self.get_status(job_id)
            stage = status.get("stage")
            message = status.get("message", "")
            progress = status.get("progress", 0)
            
            if stage == "queued" and status_callback:
                current_queue_position = status.get("queue_position")
                if stage != last_stage or current_queue_position != queue_position:
                    if current_queue_position is not None:
                        queue_message = f"Ожидание в очереди (позиция #{current_queue_position})..."
                    else:
                        queue_message = message or "Ожидание в очереди..."
                    status_callback("queued", queue_message, 5)
                    last_stage = stage
                    queue_position = current_queue_position
            elif status_callback and stage != "complete":
                if stage != last_stage or progress != last_progress or message != last_message:
                    if stage == "diarization":
                        scaled_progress = 10
                    elif stage == "transcription":
                        scaled_progress = 25
                    else:
                        scaled_progress = progress
                    
                    status_callback(stage, message, scaled_progress)
                    last_stage = stage
                    last_progress = progress
                    last_message = message

            if status.get("status") == "complete":
                result = await self.get_result(job_id)
                logger.info("Audio Pipeline: результат получен")
                return result

            if status.get("status") == "error":
                raise RuntimeError(status.get("message", "Ошибка обработки"))

            await asyncio.sleep(poll_interval)


audio_pipeline_client = AudioPipelineClient()
