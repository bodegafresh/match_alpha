import json
from typing import Any

from app.core.time import utc_now
from app.db.repositories.base import Repository


class ObservabilityRepository(Repository):
    async def start_pipeline(self, job_name: str, payload: dict[str, Any] | None = None) -> str | None:
        row = await self.fetch_one(
            """
            insert into pipeline_runs (job_name, status, payload)
            values (:job_name, 'STARTED', cast(:payload as jsonb))
            returning pipeline_run_id::text
            """,
            {"job_name": job_name, "payload": json.dumps(payload or {})},
        )
        return row["pipeline_run_id"] if row else None

    async def finish_pipeline(
        self,
        pipeline_run_id: str | None,
        status: str,
        records_processed: int = 0,
        payload: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> None:
        if not pipeline_run_id:
            return
        await self.execute(
            """
            update pipeline_runs
            set status = :status,
                finished_at = :finished_at,
                records_processed = :records_processed,
                payload = cast(:payload as jsonb),
                error_message = :error_message
            where pipeline_run_id = :pipeline_run_id
            """,
            {
                "pipeline_run_id": pipeline_run_id,
                "status": status,
                "finished_at": utc_now(),
                "records_processed": records_processed,
                "payload": json.dumps(payload or {}),
                "error_message": error_message,
            },
        )

    async def data_quality_event(
        self,
        layer: str,
        severity: str,
        check_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
    ) -> None:
        await self.execute(
            """
            insert into data_quality_events
              (layer, entity_type, entity_id, severity, check_type, message, payload)
            values
              (:layer, cast(:entity_type as entity_type), :entity_id, cast(:severity as severity_level),
               :check_type, :message, cast(:payload as jsonb))
            """,
            {
                "layer": layer,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "severity": severity,
                "check_type": check_type,
                "message": message,
                "payload": json.dumps(payload or {}),
            },
        )
