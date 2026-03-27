##########################################################################
ObsForge: a metadata enrichment service for Rubin Observatory observations
##########################################################################

Overview
========

ObsForge is a metadata enrichment service for Rubin Observatory observations.

The ObsForge enrichment workflow operates on Prompt Processing visits, the first data products released by Rubin Observatory after an observation is taken.
These visits are subject to an 80-hour embargo period and are released by the Prompt Publication service once the embargo expires.
When a visit is released, the Prompt Publication service sends a notification to ObsForge, which triggers the enrichment workflow.


The service consists of:

- A FastAPI application that accepts registration/status requests and manages durable job state;
- Arq workers that perform asynchronous enrichment jobs for Prompt Publication visits;
- Quix stream-processing pipelines that consume Telemetry streams from Sasquatch, apply transformations and write telemetry summary tables to ObsDB;
- ObsDB a PostgreSQL database that stores both durable job state and the generated metadata products;
- ObsForge TAP server.

ObsForge integrates with:

- The prompt Butler data repository exposed by the Prompt Publication service, which provides visit metadata through dataset types such as ``preliminary_visit_image`` and ``preliminary_visit_summary``.

.. note::

   ObsForge may require specific dataset types to be included in the Prompt Publication Butler repository.
   See `RFC-1134 <https://rubinobs.atlassian.net/browse/RFC-1134>`_ and `Prompt Publication Service Roadmap <https://rubinobs.atlassian.net/wiki/spaces/~david.irving/pages/1434910781/Prompt+Publication+Service+Roadmap>`_ for additional details.

- Sasquatch, which provides the observatory telemetry streams.

ObsForge produces several metadata products, including:

- An ObsCore table for IVOA-compliant services
- A Visit summary table
- Telemetry summary tables

These products are stored in ObsDB and exposed to users through TAP, the IVOA standard protocol for accessing astronomical data.


The following diagram illustrates the architecture and data flow of ObsForge, showing the interactions between the Prompt Publication service, ObsForge components, the Prompt Butler and Sasquatch.
The arq workers populate the ObsCore and visit tables, while Quix stream-processing pipelines populate the telemetry summary tables.
Visit and telemetry association enables telemetry data to be linked to the corresponding visit. 

.. mermaid::
   :caption: ObsForge architecture and data flow.

   flowchart TD
      Prompt["Prompt<br/>Publication<br/>service"]
      Butler[("Prompt Butler")]
      Sasquatch[("Sasquatch")]
      TAP["ObsForge TAP"]

      subgraph ObsForge["ObsForge"]
         API["FastAPI<br/>/register"]
         Queue["Redis<br/>arq queue"]
         Worker["arq</br>workers"]
         StreamProcessor["Quix stream-processing</br>pipelines"]
      end

      subgraph ObsDB["ObsDB"]
         Jobs[("Enrichment<br/>Job")]
         ObsCore[("ObsCore")]
         Visit[("Visit<br/>summary")]
         Telemetry[("Telemetry<br/>summary")]
      end

      Prompt --> API

      API -->|"Visit registration"| Jobs
      API -->|"Enqueue<br>enrichment task"| Queue
      Queue -->|"Run enrichment"| Worker
      Worker -->|"Phase updates"| Jobs

      Worker -.->|"Retrieve visit metadata"| Butler
      StreamProcessor -.->|"Retrieve telemetry streams"| Sasquatch

      Worker -->|"Insert ObsCore rows"| ObsCore
      Worker -->|"Insert visit rows"| Visit
      StreamProcessor -->|"Process telemetry streams"| Telemetry

      ObsCore --> TAP
      Visit --> TAP
      Telemetry --> TAP

      classDef big font-size:22px;
      classDef medium font-size:20px;
      class ObsForge big;
      class ObsDB big;
      class Prompt medium;
      class Butler medium;
      class Sasquatch medium;
      class TAP medium;


A simplified diagram of the ObsDB data model is shown below, illustrating the relationships between enrichment jobs, visits, ObsCore, and telemetry tables. 
All metadata products can be joined through ``visit_id``.

.. mermaid::
   :caption: Simplified diagram of the ObsDB data model

   erDiagram
      enrichment_job ||--o{ Visit : creates
      Visit ||--o{ ObsCore : has
      Visit ||--o{ Telemetry : has

      enrichment_job {
          int id PK
      }

      Visit {
          int id PK
          string visit
          int job_id FK
      }

      ObsCore {
          string obs_id PK
          int visit_id FK
      }

      Telemetry {
          timestamptz timestamp PK
          int visit_id FK
      }


Implementation phases
=====================

The service will be implemented in two phases:

Phase 1: Enrichment workflow and ObsCore
----------------------------------------

Phase 1 implements the enrichment workflow and populates the ObsCore table exposed through TAP for IVOA-compliant services.

The enrichment workflow incrementally populates ObsCore as Prompt Processing visits are registered in ObsForge. 
Visit registration enqueues an arq job that retrieves the corresponding ObsCore rows from the Butler, inserts those rows into ObsDB, and records job completion or failure.

Phase 2: Summarized Visit and Observatory telemetry tables
----------------------------------------------------------

Phase 2 populates Visit and Observatory telemetry summary tables in ObsDB and exposes them through TAP.
A dedicated schema design will be used to avoid excessively wide tables.

Enrichment Workflow design
==========================

Visit registration
------------------

The ObsForge enrichment workflow operates on Prompt Processing visits.
When a visit is released, the Prompt Publication service sends a notification to ObsForge, which triggers the enrichment workflow.

The notification payload contains enough information to identify a visit and query the relevant metadata from the Butler and the EFD:

.. code-block:: json

   {
     "instrument": "LSSTCam",
     "visit": 2026010800095,
     "day_obs": 20260108,
     "datasets": [
        {
          "dataset_type": "preliminary_visit_image",
          "id": "019ba0a6-0173-765f-bf27-56884ff9342a"
        },
        {
          "dataset_type": "preliminary_visit_image",
          "id": "019ba0a5-fe48-7c7a-8c3f-540057f026c3"
        },
        {
          "dataset_type": "preliminary_visit_image",
          "id": "019ba0a5-fe56-7fe8-b6c3-82991b2633c0"
        },
        {
          "dataset_type": "visit_summary",
          "id": "019ba0a5-fe64-7f6e-bb3f-4c8d1c9e2b3a"
         }
     ],
     "timespan": {
        "begin": "2026-01-09T02:45:51Z",
        "end": "2026-01-09T02:46:26Z"
     }
   }

- the ``instrument`` name and ``visit`` pair uniquely identify an observation in ObsForge.
- ``day_obs`` is included to support basic operational lookup.
- ``datasets`` is a list of dataset types and IDs (UUIDs) to query the Butler. ``preliminary_visit_image`` and the ``preliminary_visit_summary`` are the first supported dataset types, in the future the notification payload may include others. 
- the visit ``timespan`` is provided to support the visit and telemetry association step in ObsForge.


Job queue design
----------------

ObsForge uses Safir's arq integration as the transport layer between the FastAPI registration API and the asynchronous enrichment workers.
Redis provides the transient queue for the enrichment workers while PostgreSQL stores the durable job state.

This separation is intentional:

- PostgreSQL is the source of truth for the durable job state, registration payload, failure summary.
- Redis is only the arq transport and does not define whether an observation has been durably registered, completed, or failed.
- The public API returns durable job state, optionally overlaid with live arq status for queued or running jobs.


The ``enrichment_job`` table
----------------------------

ObsForge PostreSQL ``enrichment_job`` table records enough metadata to support retries, idempotent upserts, and operational debugging.

This table is intentionally ObsForge-specific even though its phase vocabulary follows a subset of IVOA UWS execution phases:

- ``PENDING``: the observation has been registered in the database but has not yet been queued for execution.
- ``QUEUED``: the arq job has been queued but a worker has not yet started enrichment.
- ``EXECUTING``: a worker is actively enriching the observation.
- ``COMPLETED``: enrichment completed and the output records were inserted.
- ``ERROR``: enrichment failed after a permanent error or after retries were exhausted.

This implementation avoids UWS phases that ObsForge does not yet need, such as ``HELD``, ``SUSPENDED``, ``ABORTED``, ``ARCHIVED``, and ``UNKNOWN``.
See also Appendix A on atomic phase transitions.

The initial schema for the ``enrichment_job`` table includes:

.. csv-table::
   :header: "Column", "Description"

   "``id``", "Primary key passed to ``run_enrichment(job_id)``."
   "``instrument``", "Instrument name from the registration payload."
   "``visit``", "Visit identifier from the registration payload."
   "``day_obs``", "Observation day from the registration payload."
   "``registration_payload``", "JSONB copy of the inbound registration payload for replay and debugging."
   "``arq_job_id``", "Internal arq transport job identifier, nullable until the durable job has been enqueued."
   "``phase``", "Current job phase."
   "``error_code``", "Most recent failure code, nullable for non-failed jobs."
   "``error_message``", "Most recent failure message, nullable for non-failed jobs."
   "``created_at``", "UTC timestamp for job creation."
   "``updated_at``", "UTC timestamp for the most recent job update."
   "``started_at``", "UTC timestamp for the start of enrichment execution."
   "``completed_at``", "UTC timestamp for enrichment completion."

Safir/arq integration
---------------------

The FastAPI application initializes Safir's ``arq_dependency`` during application startup using the configured arq mode and Redis settings.
Request handlers depend on that queue and wrap it with an ``EnrichmentQueueStore`` adapter.
The adapter centralizes all arq-specific calls:

- ``enqueue(job_id)`` enqueues ``run_enrichment`` on the configured arq queue and returns the arq job identifier.
- ``status(arq_job_id)`` reads live arq metadata when the job is still known to Redis.
- ``succeeded(arq_job_id)`` reads the arq result when it is still available.
- ``abort(arq_job_id)`` requests arq cancellation for jobs that have not already left the queue.

The durable ``enrichment_job.arq_job_id`` column stores the arq job identifier produced by ``ArqQueue.enqueue``.
This value is internal transport metadata; it is useful for status overlay, abort requests, and operational diagnostics, but is not part of the public serialized job response.

Observation registration follows this sequence:

#. The handler validates the Prompt Publication payload and asks ``EnrichmentJobService`` to register the observation.
#. If the observation is new, the service creates a durable ``PENDING`` job through ``EnrichmentJobStore``.
#. If the observation already exists in ``PENDING``, ``QUEUED``, ``EXECUTING``, or ``COMPLETED``, the service returns the existing durable job without enqueueing duplicate work.
#. If the observation already exists in ``ERROR``, the service treats the duplicate registration as a retry request.
#. For a new ``PENDING`` job or an ``ERROR`` retry, the service enqueues ``run_enrichment(job_id)`` through ``EnrichmentQueueStore``.
#. The storage layer atomically stores the returned ``arq_job_id`` and transitions the durable phase to ``QUEUED``.
#. The handler returns ``202 Accepted`` with a ``Location`` header pointing to ``/obsforge/jobs/{job_id}``.

.. rst-class:: technote-wide-content mermaid-wide-content
.. mermaid::
   :caption: Observation registration sequence.

   sequenceDiagram
      autonumber

      participant Prompt as Prompt Publication
      participant Handler as /register Handler
      participant Service as EnrichmentJobService
      participant Store as EnrichmentJobStore
      participant Queue as EnrichmentQueueStore
      participant Worker as arq worker

      Prompt->>Handler: POST registration payload
      Handler->>Handler: Validate Prompt Publication payload
      Handler->>Service: register_observation(payload)
      Service->>Store: Register or load durable job

      alt New observation
         Store-->>Service: PENDING job
         Service->>Queue: enqueue run_enrichment(job_id)
         Queue-->>Service: arq_job_id
         Service->>Store: Store arq_job_id and transition to QUEUED
         Store-->>Service: QUEUED job
      else Existing job in PENDING, QUEUED, EXECUTING, or COMPLETED
         Store-->>Service: Existing durable job
         Note over Service,Queue: Do not enqueue duplicate work
      else Existing job in ERROR
         Store-->>Service: ERROR job
         Note over Service: Treat duplicate registration as a retry request
         Service->>Queue: enqueue run_enrichment(job_id)
         Queue-->>Service: arq_job_id
         Service->>Store: Store arq_job_id and transition to QUEUED
         Store-->>Service: QUEUED job
      end

      Queue-->>Worker: run_enrichment(job_id)
      Service-->>Handler: Durable job
      Handler-->>Prompt: 202 Accepted\nLocation: /obsforge/jobs/{job_id}


The worker process is a separate arq worker configured with ``WorkerSettings``.
Its settings include the ``run_enrichment`` function, Redis settings, queue name, maximum retry count, startup hook, shutdown hook, and arq job-abort support.
On startup, the worker configures logging, initializes Safir's database-session dependency, and builds shared ObsCore enrichment resources from runtime configuration.
On shutdown, the worker closes the database-session dependency and removes the shared ObsCore resources from the worker context.

``run_enrichment`` receives the arq worker context and the durable ``job_id``.
It creates an ``EnrichmentJobService`` backed by ``EnrichmentJobStore`` and then:

#. marks the durable job ``EXECUTING``;
#. calls ``enrich_visit`` with the durable ``job_id``, database session, and worker context;
#. marks the durable job ``COMPLETED`` if enrichment succeeds;
#. marks the durable job ``ERROR`` with an error code and message if enrichment fails permanently, retries are exhausted, or the arq job is cancelled.

``enrich_visit`` is the worker hook that performs the ObsCore integration.
It loads the stored ``registration_payload`` for the durable job, validates it as ``VisitRegistration``, builds a ``DaxObsCoreAdapter`` from the worker context, and retrieves ObsCore records for matching datasets.
For the first integration, matching datasets are registration payload entries with ``dataset_type`` set to ``preliminary_visit_image``.
The adapter constrains ``lsst.dax.obscore`` by the dataset UUIDs from those entries and returns ``ObsCoreUpsert`` records.
The worker then upserts each record into the ``ivoa.ObsCore`` table through ``ObsCoreService`` and ``ObsCoreStore``.


.. rst-class:: technote-wide-content mermaid-wide-content
.. mermaid::
   :caption: Worker enrichment sequence.

   sequenceDiagram
      autonumber

      participant Arq as arq worker
      participant Task as run_enrichment
      participant Service as EnrichmentJobService
      participant Store as EnrichmentJobStore
      participant Hook as enrich_visit
      participant Adapter as DaxObsCoreAdapter
      participant Butler as Prompt Butler
      participant ObsCore as ObsCore storage

      Arq->>Task: run_enrichment(ctx, job_id)
      Task->>Service: Create service backed by EnrichmentJobStore
      Task->>Service: mark_executing(job_id)
      Service->>Store: Transition durable job to EXECUTING
      Store-->>Service: EXECUTING job

      Task->>Hook: enrich_visit(job_id, session, ctx)
      Hook->>Store: Load durable job
      Store-->>Hook: registration_payload
      Hook->>Adapter: iter_visit_records(registration)
      Adapter->>Butler: Query preliminary_visit_image UUIDs
      Butler-->>Adapter: ObsCore source records
      Adapter-->>Hook: ObsCoreUpsert records
      Hook->>ObsCore: Upsert ObsCore records
      ObsCore-->>Hook: Rows inserted or updated
      Hook-->>Task: Enrichment complete

      alt Enrichment succeeds
         Task->>Service: mark_completed(job_id)
         Service->>Store: Transition durable job to COMPLETED
         Store-->>Service: COMPLETED job
         Task-->>Arq: Complete arq job
      else Permanent failure, retries exhausted, or arq cancellation
         Task->>Service: mark_failed(job_id, error_code, error_message)
         Service->>Store: Transition durable job to ERROR
         Store-->>Service: ERROR job
         Task-->>Arq: Raise terminal failure
      end


ObsForge relies on arq's built-in job retry mechanism to handle transient errors such as network timeouts when fetching metadata from external systems.
The durable job remains ``EXECUTING`` while arq owns the retry sequence, unless a later attempt succeeds and marks it ``COMPLETED`` or the final attempt fails and marks it ``ERROR``.
The worker compares arq's ``job_try`` value in the worker context with the configured enrichment retry limit.
If the retryable failure happens before the final configured attempt, ``run_enrichment`` re-raises arq's ``Retry`` exception so that arq can schedule the next attempt.
If it happens on the final configured attempt, ``run_enrichment`` first marks the durable job ``ERROR`` with ``error_code`` set to ``RetriesExhausted`` and then raises a non-retry exception so that arq records a terminal failed result rather than scheduling another attempt.

Because arq metadata and results are transient, GET ``/obsforge/jobs/{job_id}`` must not depend on Redis to reconstruct the workflow.
If the stored job has an ``arq_job_id`` and arq still has metadata for it, the service may present a live overlay such as ``EXECUTING`` for an in-progress arq job or ``COMPLETED``/``ERROR`` for a completed arq result.
If arq no longer has metadata, the service returns the durable PostgreSQL phase.
This makes Redis loss or arq result expiration an operational issue for live status only, not a loss of ObsForge workflow state.


API endpoints
=============

ObsForge is implemented as a FastAPI application.
The external API is mounted at the configured ``path_prefix``, which defaults to ``/obsforge``.


External API endpoints
----------------------

These endpoints will use a new scope, ``write:obsforge``, to control access to the registration and job management endpoints.

.. list-table:: External API routes
   :header-rows: 1
   :widths: 12 28 18 42

   * - Method
     - Path
     - Status
     - Description
   * - ``POST``
     - ``/obsforge/register``
     - ``202 Accepted``
     - Register one Prompt Processing observation for asynchronous enrichment.
   * - ``GET``
     - ``/obsforge/jobs/{job_id}``
     - ``200 OK`` or ``404 Not Found``
     - Return the durable enrichment job state, with live arq state overlaid when available.
   * - ``DELETE``
     - ``/obsforge/jobs/{job_id}``
     - ``204 No Content`` or ``404 Not Found``
     - Abort an enrichment job that can still be cancelled through arq.

``POST /obsforge/register``
^^^^^^^^^^^^^^^^^^^^^^^^^^^

The registration endpoint accepts the ``VisitRegistration`` request body described above and returns a serialized enrichment job.
The request creates a durable job if the ``instrument`` and ``visit`` pair has not been seen before, or returns the existing job for duplicate registrations.
If the stored job does not already have an arq transport job, ObsForge enqueues ``run_enrichment(job_id)`` and transitions the durable phase to ``QUEUED`` before returning.
If the stored job is ``ERROR``, ObsForge enqueues a new arq transport job, replaces the stored ``arq_job_id``, clears the previous failure fields, and transitions the durable phase back to ``QUEUED`` before returning.

The response includes a ``Location`` header pointing to the job resource:

.. code-block:: text

   Location: /obsforge/jobs/{job_id}

The response body has the following shape:

.. code-block:: json

   {
      "id": 42,
      "instrument": "LSSTCam",
      "visit": 2026010800095,
      "day_obs": 20260108,
      "phase": "QUEUED",
      "registration_payload": {
         "instrument": "LSSTCam",
         "day_obs": 20260108,
         "visit": 2026010800095,
         "datasets": [
            {
               "dataset_type": "preliminary_visit_image",
               "id": "019ba0a6-0173-765f-bf27-56884ff9342a"
            }
         ],
         "timespan": {
            "begin": "2026-01-09T02:45:51Z",
            "end": "2026-01-09T02:46:26Z"
         }
      },
      "created_at": "2026-01-09T02:45:51Z",
      "updated_at": "2026-01-09T02:45:51Z",
      "started_at": null,
      "completed_at": null,
      "error_code": null,
      "error_message": null
   }

All fields are included in the job response and some might be ``null`` as applicable.

``GET /obsforge/jobs/{job_id}``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The job endpoint returns the same ``SerializedEnrichmentJob`` representation as the registration endpoint.
The response is based on the durable PostgreSQL job row.
If the job has an arq transport identifier and Redis still has live metadata for that arq job, the service may overlay transient queue state:

- arq ``in_progress`` can be reported as ``EXECUTING`` for a durably ``QUEUED`` job.
- arq ``complete`` can be reported as ``COMPLETED`` or ``ERROR`` for an otherwise in-flight durable job, depending on the arq result.

If the job ID is unknown, the endpoint returns ``404 Not Found``.

``DELETE /obsforge/jobs/{job_id}``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The delete endpoint requests cancellation of the associated arq job.
When cancellation succeeds, ObsForge marks the durable job ``ERROR`` with ``error_code`` set to ``JobAborted`` and returns ``204 No Content``.
If the durable job is unknown, the job has no arq transport identifier, or arq cannot cancel the transport job, the endpoint returns ``404 Not Found``.


The ObsCore table
=================

The IVOA `ObsCore <https://www.ivoa.net/documents/ObsCore/>`_ data model is focused on describing the core metadata common to most data products distributed for astronomical observations. 
Observations are searched and discovered via TAP.

The ``lsst.dax.obscore`` package implements the ObsCore data model for Rubin Observatory data products, and is used by ObsForge to retrieve ObsCore records from the Butler.
ObsForge uses the `prompt.yaml <https://raw.githubusercontent.com/lsst-dm/dax_obscore/refs/heads/main/configs/prompt.yaml>`_ configuration and populates the ObsCore table with one ``preliminary_visit_image`` dataset per row. 

Appendix B describes the ObsCore columns in the final configuration.

ObsForge owns the ObsDB database schema, and so it is responsible for creating and evolving the ObsCore table.

ObsCore SQLAlchemy schema implementation
----------------------------------------

The ObsCore table schema is implemented in ObsForge with SQLAlchemy as ``obsforge.schema.ObsCore``.

Database constraints for nullable columns follow the description in Appendix B.
The ObsCore type names used in this document map to SQLAlchemy and PostgreSQL types as follows:

.. list-table:: ObsCore type mapping
   :header-rows: 1

   * - **ObsCore type**
     - **SQLAlchemy type**
     - **PostgreSQL type**
   * - ``string``
     - ``Text``, via ``SchemaBase.type_annotation_map`` for ``Mapped[str]``
     - ``text``
   * - ``text``
     - ``Text``, via ``SchemaBase.type_annotation_map`` for ``Mapped[str]``
     - ``text``
   * - ``int``
     - ``Integer``
     - ``integer``
   * - ``long``
     - ``BigInteger``
     - ``bigint``
   * - ``double``
     - ``Float``
     - ``double precision``

SQLAlchemy ``Column.info`` is used to preserve ObsCore metadata that is not represented by the SQL type system: unit, description, and UCD.
For example, the ``calib_level`` column carries all the semantic metadata required by the ObsCore data model and ObsTAP protocol in its ``info`` dict:

.. code-block:: python

   calib_level_column = ObsCore.__table__.columns["calib_level"]
   assert calib_level_column.info == {
       "unit": "",
       "description": "Calibration level of the observation: in {0, 1, 2, 3, 4}",
       "ucd": "meta.code;obs.calib",
   }

This ``Column.info`` metadata can be used, for example, to export the ObsCore schema into the ``sdm_schemas`` YAML format which is used to create the corresponding TAP schema.

The ``ObsCoreUpsert`` Pydantic model contains all the fields that ObsForge writes during enrichment.

``SerializedObsCore`` represents the full ObsCore record returned by storage.

ObsCore records are retrieved from the ObsForge ObsCore adapter and inserted into the ``ivoa.ObsCore`` table in ObsDB by the enrichment workflow when an observation is registered.


The ObsCore adapter
-------------------

The enrichment workflow uses the ObsCore adapter to retrieve ObsCore records from the Butler and insert them into the ``ivoa.ObsCore`` table in ObsDB as part of the enrichment process.

The ``ObscoreExporter.iter_records()`` method was added to ``lsst.dax.obscore`` as a public interface to iterate over ObsCore records as Python objects.

ObsForge uses the ``prompt.yaml`` configuration to build an ``ObscoreExporter`` instance in the worker context.
For a given observation, ObsForge selects ``preliminary_visit_image`` datasets from the registration payload and uses the dataset IDs to constraint the Butler query.

ObsForge also overrides the following configuration defaults in the ObsCore adapter:

- Change the ``obs_id`` formatter to use the dataset UUIDs and use this column as primary key for the ``ivoa.ObsCore`` table.
- Add the ``visit_id`` extra column and use it as foreign key to make it easier to join ObsCore rows with the ObsDB ``visits`` table. 
- drop ``lsst_tract`` and ``lsst_patch`` extra columns since ``tract`` and ``patch`` are coadd concepts and are not part of the ``preliminary_visit_image`` data ID in the Butler.
- drop ``lsst_visit``, ``lsst_band``, and ``lsst_filter`` extra columns since they are redundant with  ``visit_id``, ``band`` and ``physical_filter`` columns in the ObsDB ``visits`` table.

The ObsCore adapter is hooked in the worker's ``enrich_visit()`` function.

The Visit table
===============

TDB


Telemetry summary tables
========================

ObsForge uses `Quix Streams <https://github.com/quixio/quix-streams>`_, a Python stream-processing framework, to process telemetry from Sasquatch
Kafka topics and write derived data to ObsDB Telemetry summary tables.

In this model, Kafka topics provide the source telemetry. 
Quix Streams can filter, join, enrich, and aggregate those records, then write the results to
ObsDB tables exposed through TAP.

Two approaches were considered for writing the result of Quix stream-processing after joining or enriching Kafka streams.

Approach 1: Publish joined result back to Kafka
-----------------------------------------------

In this approach, Quix consumes two or more Kafka topics, joins or enriches the records in Python, then publishes the resulting records to a new Kafka topic.
A separate consumer or sink process then writes that derived topic into ObsDB.

This approach has the following advantages:

- Creates a reusable enriched Kafka topic for multiple downstream consumers.
- Preserves the joined or enriched result as an event stream.
- Supports replay by re-consuming the derived Kafka topic.
- Decouples stream processing from PostgreSQL ingestion.
- Provides a clear Kafka-level contract if the Avro schema is explicitly defined and registered.
- Better fits cases where other services need the enriched data.

This approach has the following disadvantages:

- Requires defining and maintaining an Avro schema for the resulting joined topic.
- Requires Schema Registry registration and compatibility checks for the derived schema.
- Increases the number of Kafka topics to manage adding more load to Kafka.
- Requires an additional sink or consumer to load the derived topic into PostgreSQL.
- Introduces more moving parts, deployment coordination, and failure modes.
- Can duplicate schema ownership between Avro, SQLAlchemy, and PostgreSQL.

Approach 2: Write joined result directly to PostgreSQL
------------------------------------------------------

In this approach, Quix consumes the source Kafka topics, performs the join or enrichment in Python, shapes the resulting dictionary to match the target table, and writes directly to ObsDB using ``PostgreSQLSink``.

This approach has the following advantages:

- Avoids creating a new Kafka topic for the joined result.
- Avoids defining and registering an Avro schema for the derived output.
- Simplifies the architecture and ObsForge dependency on Sasquatch.
- SQLAlchemy and Alembic remain the source of truth for the PostgreSQL table schema.
- Fits cases where PostgreSQL is the end destination for the enriched data.

This approach has the following disadvantages:

- The enriched result is not available as a reusable Kafka stream.
- Other downstream consumers cannot independently consume the joined result from Kafka.
- Replay requires reprocessing the original input topics rather than replaying a derived topic.
- Tightly couples the stream-processing to PostgreSQL.

For ObsForge telemetry products, the preferred approach is to write joined or enriched results directly to ObsDB since it is the end destination.
In this model, SQLAlchemy and Alembic own the database schema, and the Quix pipeline explicitly shapes output rows to match that schema.

Quix Streams integration
------------------------

The ObsForge integration implements the direct-to-PostgreSQL approach as one long-running Quix Streams process for each pipeline.

A stream-processing pipeline has the following configuration:

- Pipeline name
- Consumer group
- Source topics
- Target schema and table
- Transformations

The process is started from the ObsForge container with ``obsforge
process-stream --stream-config-path config/streams/pipeline.yaml``.

Schema ownership
^^^^^^^^^^^^^^^^

The implementation keeps three related contracts separate:

.. list-table::
   :header-rows: 1

   * - Contract
     - Authority
     - Decision
   * - Avro schema
     - Sasquatch Schema Registry
     - Quix fetches the corresponding Avro schema at runtime.  ObsForge does
       not copy the Avro schema.
   * - ObsDB schema
     - ObsForge SQLAlchemy
     - ObsForge creates and evolves the schema.
       The stream process is not allowed to evolve the schema.
   * - Routing and transformations
     - Versioned ObsForge pipeline YAML
     - A pipeline selects its topics, consumer group, target table, and specifies transformations.

The PostgreSQL sink is configured with ``schema_auto_update=False`` so that SQLAlchemy and Alembic remain the only authorities for the ObsDB schema.  
The runner also rejects a sink schema and table that are absent from ObsForge's SQLAlchemy metadata.

Sink metadata is disabled with ``include_metadata=False``.
The Quix PostgreSQL sink would otherwise add a Kafka record timestamp named ``timestamp``, which collides with the Sasquatch payload field of the same name.


.. Quix Streams 3.25 issues ``CREATE SCHEMA IF NOT EXISTS`` during PostgreSQL sink setup even when schema auto-update is disabled.  Alembic creates the schema first, but this statement may require more privileges than a future insert-only stream role should have.  This must be resolved in the upstream connector or a tested ObsForge adapter before separating migration and writer database roles.


Pipeline configuration and deployment
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Pipeline YAML is treated as application code because its target table and schema must agree with SQLAlchemy models and Alembic migrations.  
One YAML file per pipeline lives under ``config/streams/`` and is packaged into the ObsForge container image.  

Production deployments should apply the matching Alembic migrations before starting the stream process.

An example of a stream-processing pipeline is shown below:

.. mermaid::
   :caption: Stream-processing pipeline example
   
   flowchart TD
    A["Sasquatch</br>lsst.sal.Scheduler.observatoryState"]
    B["Quix AvroDeserializer"]
    C["Schema Registry"]
    D["Quix PostgreSQLSink"]
    E[("ObsDB</br>scheduler.observatory_state")]

    A -.-> B
    B -.-> C
    B -->|"drop_fields(...),<br/>epoch_seconds_to_datetime(...)"| D
    D -->|"INSERT only"| E


The YAML pipeline definition for this example would be:

.. code-block:: yaml

   version: 1
   name: scheduler-observatory-state
   consumer_group: obsforge-scheduler-observatory-state-v1

   source:
     topic: lsst.sal.Scheduler.observatoryState

   sink:
     schema: scheduler
     table: observatory_state

   transformations:
     - operation: drop_fields
       fields:
         - salIndex
       prefixes:
         - private_
     - operation: epoch_seconds_to_datetime
       field: timestamp
       input_scale: tai

In this example, ``drop_fields`` removes ``salIndex`` and fields with the
``private_`` prefix.  ``epoch_seconds_to_datetime`` converts the source
Scheduler timestamp from TAI Unix seconds to a timezone-aware UTC ``datetime``.


Transformations
^^^^^^^^^^^^^^^

The following transformations are supported in the initial ObsForge integration:

- ``drop_fields``: drop fields by name or prefix.
- ``epoch_seconds_to_datetime``: convert TAI or UTC epoch seconds to a
  timezone-aware UTC ``datetime``.  The pipeline specifies the input time scale
  with ``input_scale: tai`` or ``input_scale: utc``.


Scaling
^^^^^^^

The default scaling unit is one process per pipeline, which can be replicated to increase throughput.
Each pipeline has a consumer group; replicas of that pipeline share the group and can process partitions in parallel, while unrelated pipelines use different groups.
Changing a consumer-group identifier cause records to be consumed again.

Although one Quix ``Application`` can host multiple data frames, those streams share a consumer loop and checkpoint lifecycle, and sink flushes are sequential.
The initial design therefore avoids combining unrelated topics in one process: a slow or failing PostgreSQL sink should not delay or replay work for every telemetry pipeline. 

Each PostgreSQL sink instance and pipeline replica owns a database connection in addition to the API and worker pools.
PostgreSQL will likely become the limiting resource before Kafka for high-volume telemetry.


Failure behavior
^^^^^^^^^^^^^^^^

Changes in the target schema requires a corresponding SQLAlchemy change and Alembic migration before the updated pipeline is deployed.  

Removed required fields and incompatible type changes fail visibly at the sink rather than silently mutating the table.  
CI should load every pipeline definition, validate its sink target against SQLAlchemy metadata before production deployment.


Visit and Telemetry association
-------------------------------

The enrichment workers populate the Visit table, while Quix stream-processing pipelines populate the telemetry summary tables.

Each telemetry table has a timestamp column and a ``visit_id`` column that is initially NULL.

Telemetry rows represent individual measurements or aggregation windows, and their timestamps are used to determine the visit association.
We assume that aggregation windows are shorter than the typical visit timespan, ensuring that they can be fully contained within a single visit for association.

We envision two mechanisms for visit association:

1. Normally, telemetry is recorded before the visit is registered. In this case, the visit registration triggers an asynchronous worker to perform the visit association step.

2. A periodic reconciliation step is needed to associate new telemetry recorded after the visit registration. This is expected to happen when the ObsDB schema evolve to include new telemetry tables.

The periodic reconciliation step should also include monitoring for potential issues:

- Unmatched older telemetry
- Association lag 

The visit association step updates the ``visit_id`` column in the telemetry tables based on the visit time span.

.. code:: sql

   UPDATE telemetry
   SET visit_id = :visit_id
   FROM visit
   WHERE telemetry.visit_id IS NULL
     AND telemetry.timestamp >= visit.start_time
     AND telemetry.timestamp < visit.end_time;

This SQL is implemented in a ``TelemetryAssociationStore`` and orchestrated in a ``TelemetryAssociationService``, following ObsForge's existing service/storage layering.


Appendix A: Atomic phase transitions
====================================

Job phase transitions are triggered by the registration handler and the worker code, and are subject to the following workflow rules:

- Update ``PENDING -> QUEUED`` after first-time registration; for ``QUEUED``, return the current job unchanged; reject ``EXECUTING`` and ``COMPLETED``.
- Update ``ERROR -> QUEUED`` when duplicate registration is used as a user-facing retry. This transition stores the new ``arq_job_id``, clears ``error_code`` and ``error_message``, clears ``started_at`` and ``completed_at``, and updates ``updated_at``.
- Update ``PENDING | QUEUED -> EXECUTING``; return unchanged if already ``EXECUTING``; reject ``COMPLETED`` and ``ERROR``.
- Update ``EXECUTING -> COMPLETED``; return unchanged if already ``COMPLETED``; reject all other phases.
- Update ``PENDING | QUEUED | EXECUTING -> ERROR``; return unchanged if already ``ERROR``; reject ``COMPLETED``.

Instead of reading the Job current phase in the service layer and updating the Job phase in the storage layer in separate transactions,
it is safer to implement atomic phase transitions in the storage layer to avoid race conditions, while keeping the service layer thin.

This can be implemented with a conditional ``UPDATE`` statement that includes the allowed current phase(s) in the ``WHERE`` clause, and returns the updated row.

.. code:: sql

   UPDATE enrichment_job
   SET phase = 'COMPLETED', ...
   WHERE id = :job_id
      AND phase IN ('EXECUTING')
   RETURNING ...

The storage layer can centralize this pattern in a private transition helper, such as ``_transition(job_id, requested, allowed_current, idempotent_current, values)``.

The helper should first attempt the conditional ``UPDATE`` and return the updated row when the current phase matches ``allowed_current``.
If no row is updated, it should fetch the current job in the same transaction.

The ``requested`` argument is the target phase and it is used for diagnostics when the transition is illegal.
The ``allowed_current`` argument is the set of source phases that may be changed by the SQL update.
The ``idempotent_current`` argument is the set of phases where the operation is already effectively complete or should be treated as a harmless no-op, so the current row can be returned unchanged instead of raising an error.

For example, update to ``COMPLETED`` should allow only ``EXECUTING`` as ``allowed_current``, use ``COMPLETED`` as ``idempotent_current``, and raise an invalid-transition error for any other current phase.

.. code-block:: python

    @retry_async_transaction
    async def mark_completed(self, job_id: int) -> SerializedEnrichmentJob:
        """Mark a job as completed."""
        async with self._session.begin():
            now = self._now_for_db()
            return await self._transition(
                job_id,
                requested=EnrichmentJobPhase.COMPLETED,
                allowed_current=(EnrichmentJobPhase.EXECUTING,),
                idempotent_current=(EnrichmentJobPhase.COMPLETED,),
                values={
                    "phase": EnrichmentJobPhase.COMPLETED,
                    "started_at": func.coalesce(
                        SQLEnrichmentJob.started_at, now
                    ),
                    "completed_at": now,
                    "updated_at": now,
                },
            )

Database session is initialized with ``REPEATABLE READ`` isolation level to ensure that each transaction sees a consistent snapshot of the database, even if other transactions are concurrently modifying the same data.
That is useful for job state machines, duplicate registration handling, and workflow transitions where we want decisions to be based on a stable database view.

The tradeoff is that PostgreSQL may raise serialization/concurrency errors due to transaction conflicts. 
Safir has the ``@retry_async_transation`` decorator to handle async transaction retries.
See `Retrying database transactions <https://safir.lsst.io/user-guide/database/retry.html>`_ for more information.


Appendix B: The ObsCore data model
==================================

This section describe the ObsCore columns as implemented in ObsForge.

Mandatory ObsCore columns for ObsTAP
------------------------------------

Mandatory ObsCore columns in the ``ivoa.ObsCore`` table. 

.. csv-table:: ObsCore fields for ObsTAP
   :file: ./obscore_columns.csv
   :header-rows: 1


Observation information
^^^^^^^^^^^^^^^^^^^^^^^

- ``dataproduct_type`` - Data product (file content) primary type. E.g. ``image`` for Prompt Processing visit-images. 
- ``dataproduct_subtype`` 	Data product specific type. Added here to distinguish between different types of data products. E.g. ``lsst.visit_image`` for Prompt Processing preliminary visit-images.
- ``calib_level`` - Calibration level of the observation: in {0, 1, 2, 3, 4}. E.g. ``2`` for Prompt Processing ``preliminary_visit_image`` since they are calibrated data products.

Target information
^^^^^^^^^^^^^^^^^^

- ``target_name`` - Object of interest. Can be used to specify an observation field e.g. ``ddf_ecdfs`` for the Extended Chandra Deep Field South pointing, or ``NULL`` for non-targeted observations.

Data description
^^^^^^^^^^^^^^^^

- ``obs_id`` - Internal ID given by the ObsTAP service. Formatted like ``{id}``, using the globally unique Butler dataset UUIDs, e.g. ``019ba0a6-0173-765f-bf27-56884ff9342a``. Used as primary key in ObsForge ObsCore table.
- ``obs_collection`` - Name of the data collection. E.g. ``LSST.Prompt``, in ObsCore a given observation can only be in a single collection so we cannot use Butler collection names here. 

Curation information
^^^^^^^^^^^^^^^^^^^^

- ``obs_publisher_did`` - ID for the Dataset given by the publisher. Formatted like ``"ivo://org.rubinobs/usdac/lsst-prompt?repo=prompt&id={id}"`` where ``{id}`` is the visit image UUID in Butler. E.g. ``'https://data.lsst.cloud/api/datalink/links?ID=ivo%3A%2F%2Forg.rubinobs%2Flsst-prompt%3Frepo%3Dprompt%26id%3D019ba0a6-0173-765f-bf27-56884ff9342a``.  This is the identifier that will be used in the DataLink service to link the ObsCore record to the corresponding data products (see below).

.. Use the UUID instead?

Data access information
^^^^^^^^^^^^^^^^^^^^^^^

- ``access_url`` DataLink URL for this visit-image. Set to ``<rsp-base-url>/api/datalink/links?ID=<obs_publisher_did>`` where ``<rsp-base-url>`` is the base URL for the corresponding instance of the Rubin Science Platform where ObsForge is deployed and ``<obs_publisher_id>`` is defined above. 
- ``access_format``  Content format of the dataset.  To indicate that this is a URL to a DataLink service, the ``access_format`` column is set to ``application/x-votable+xml;content=datalink``. 
- ``access_estsize`` - Estimated size of dataset in kilobytes.  ``NULL`` for Prompt Processing visit-images.


Spatial characterization
^^^^^^^^^^^^^^^^^^^^^^^^

- ``s_ra`` and ``s_dec`` - Central Spatial Position in ICRS; 
- ``s_fov`` - Estimated size of the covered region as the diameter of a containing circle.
- ``s_region`` - Sky region covered by the data product (expressed in ICRS frame). Reported as simplified 12-vertex polygon for the camera outline on the sky.
- ``s_resolution`` - Spatial resolution of data as FWHM of PSF. ``NULL`` for Prompt Processing visit-images.
- ``s_xel1`` and ``s_xel2`` - Number of elements along the spatial axes of the data product. 

Time characterization
^^^^^^^^^^^^^^^^^^^^^

- ``t_xel`` - Number of elements along the time axis. ``NULL`` for Prompt Processing visit-images.
- ``t_min`` and ``t_max`` - start and stop time of observation, in MJD.
- ``t_exptime`` - Total exposure time, in seconds.
- ``t_resolution`` - Temporal resolution FWHM. ``NULL`` for Prompt Processing visit-images.


Spectral characterization
^^^^^^^^^^^^^^^^^^^^^^^^^

- ``em_filter_name`` - Filter name associated with the observation spectral coverage. E.g. ``g``.
- ``em_xel`` - Number of elements along the spectral axis. ``NULL`` for Prompt Processing visit-images. 
- ``em_min`` and ``em_max`` - Start and stop in spectral coordinates, in meters. Map ``em_filter_name`` to wavelengths. E.g. ``"g": [4.026e-07, 5.483e-07]``.
- ``em_res_power`` - Value of the resolving power along the spectral axis (R). ``NULL`` for Prompt Processing visit-images.

Observable axis
^^^^^^^^^^^^^^^

- ``o_ucd`` - Nature of the observable axis. E.g., ``phot.flux.density``.

Polarization characterization
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

- ``pol_xel`` - Number of elements along the polarization axis. ``NULL`` for Prompt Processing visit-image.

Provenance information
^^^^^^^^^^^^^^^^^^^^^^

- ``instrument_name`` - The name of the instrument used for the observation. E.g. ``LSSTCam``.
- ``facility_name`` - The name of the facility, telescope, or space craft used for the observation. E.g. ``Rubin:Simonyi``.

Extra columns in ObsCore
------------------------

Extra columns in the ObsCore table. 

.. csv-table:: Extra columns in the ObsCore table
   :file: ./extra_columns.csv
   :header-rows: 1

- ``obs_title`` - Brief description of dataset in free format. Formatted like ``"{dataset_type} - {band} - {records[visit].name}-{records[detector].full_name} {records[visit].timespan.begin.utc.isot}Z"``, e.g. ``'preliminary_visit_image - g - MC_O_20260108_000095-R30_S22 2026-01-09T02:45:51.712950Z'``.
- ``visit_id`` - Identifier for a specific LSSTCam pointing. E.g. ``2026010800095``. Used as foreign key to join ObsCore rows with the ObsDB ``visits`` table.
- ``lsst_detector`` - Identifier for CCD within the LSSTCam focal plane". E.g. ``125``

Appendix C: ObsDB schema 
========================

The ObsDB schema with summary telemetry tables will be designed in Phase 2.
