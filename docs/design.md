# EchoAct Requirements Specification

- Document type: A requirements baseline defining the product's features, operating policies, quality attributes, and acceptance criteria.
- Scope: Local speech generation and playback, reading-position highlighting, document and history management, REST API, MCP, and operations management.
- Technology independence: This document does not specify a UI framework, a database product, or internal processing architecture. Where it fixes a relationship between external interfaces, such as the MCP server being a client of the local REST service, that relationship is a product requirement rather than an implementation detail.
- Interpretation principles: The behaviors and limits defined in the body of this document are product requirements; items excluded from support are governed by Section 7. Implementation schedules and test results are outside the scope of this document.

## 1. Product Overview

EchoAct is a personal desktop application that generates speech from Korean and English text entered by the user, entirely on the user's PC, and either plays it back or saves it to a file. It constrains resource usage so it can be used alongside other work on a laptop, and it reuses an already-loaded model on repeated generation.

### 1.1 Users and Access Paths

- Desktop user: Enters text, listens to narration, checks the reading position, and manages documents and generation history.
- Local API client: Requests speech generation, queries status, and retrieves results within its granted permissions.
- MCP client: Uses the same local speech generation features through tool calls.
- Owner: The user of the app on the local PC. Manages server activation, integration permissions, resource budgets, and data retention policy.

### 1.2 Terminology

| Term | Definition |
| --- | --- |
| Document | A title and text saved by the user. Editing it does not change the source text of previous generation jobs. |
| Job | A generation request performed with one fixed text and one fixed set of voice settings. |
| Segment | The smallest unit of narration linking audio to source text. It is a sentence by default; long sentences may be split further. |
| Generation progress / Playback progress | The amount generated versus the actual listening position. These are different values, and reading highlight follows playback progress. |
| Warm start | Keeping the same model in memory so that loading can be skipped for the next job. |
| One-off job | A job whose result is available only for a defined period and whose source text and audio are not kept in permanent history. |
| Retained job | A job whose source text, settings, and results are continuously managed in the library with the user's consent. |

All features follow the same input validation, job states, permission, and resource policies. Differences between the GUI, REST, and MCP paths are limited to the input method and the result delivery method; auto-play, access scope, and result lifetime follow each path's stated policy.

## 2. Functional Specification

### 2.1 Text Input

| ID | Feature | Specification and verification criteria |
| --- | --- | --- |
| F-01 | Direct entry and paste | Text can be entered and edited, or clipboard contents can be pasted in. |
| F-02 | Open text file | TXT and Markdown files are read as plain text. UTF-8 and CP949 are supported, and opening a file replaces the existing input. Markdown formatting is not rendered. |
| F-03 | Input validation | The current character count and the 50,000-character maximum are displayed. Generation cannot be started with empty input, whitespace-only input, or input exceeding the limit. Files larger than 2,000,000 bytes are not opened. |

### 2.2 Model and Voice Settings

| ID | Feature | Specification and verification criteria |
| --- | --- | --- |
| F-04 | Model selection | This release ships one model, Supertonic 3, and it is both the default and the only choice. The model is nonetheless selected through the same list, manifest, and per-model budget machinery as any other, per F-63 and F-84, so that adding a model later changes data rather than structure. A model the manifest marks as unable to run within the current budget is shown as unavailable with the reason, never hidden and never silently substituted. |
| F-05 | Language selection | Auto, Korean, or English is selected. In auto mode, the language is determined per sentence based on whether it contains Hangul. |
| F-06 | Gender and voice selection | A female or male voice is selected, along with a voice available for that model and gender. Supertonic 3 provides 5 female and 5 male voices. A voice is not specific to one language, so the same voice reads both Korean and English and its quality in each is governed by N-08 rather than promised here. Where a model offers no voice for a requested language and gender combination, that is stated before generation rather than presented as a native result. |
| F-07 | Tempo setting | Tempo is set within the range 0.70x–1.50x. The default is 1.00x. The final rhythm may vary further depending on the speaking-style setting. |
| F-08 | Speaking style selection | Natural, Calm, Bright, or Narration is selected. A style is a preset over tempo, the inter-segment pause the app inserts itself per F-82, and whatever expression controls the model exposes. A style never changes which characters are spoken, and a model that cannot honor part of a style applies the rest rather than refusing. |
| F-09 | First-time model preparation | If the selected model is not present locally, the owner can download it from the GUI as an explicit action. Once downloaded, a model uses local files thereafter. First-time preparation requires an internet connection and sufficient storage space. A download triggered by a REST or MCP request is refused unless the owner authorized that model in advance, per Section 5.3. Every download resolves against the model manifest in F-84. |

### 2.3 Speech Generation and Playback

| ID | Feature | Specification and verification criteria |
| --- | --- | --- |
| F-10 | Speech generation | Speech is generated from the current input and voice settings. Long text is also processed in input order, and only one generation job runs at a time. The source text and voice settings of an in-progress job are not changed. Changes to resource policy follow F-78. |
| F-11 | Generation status display | The model preparation, loading, generating, complete, and error states, along with generation progress, are displayed. |
| F-12 | Playback during generation | Rather than waiting for full generation to finish, playback starts automatically once the first sentence's worth of audio is ready. Subsequent portions are played back continuously in the order they are generated. Playback waits at segments that are not yet ready. |
| F-13 | Playback control | Generated audio can be played, paused, and stopped. Pausing or stopping does not halt speech generation, and the user's paused state is maintained even when new audio is generated. |
| F-14 | Playback position seeking | The playback position can be changed within already-generated segments. The current playback time and the total playable length are displayed. Ungenerated segments cannot be sought to. |
| F-15 | Cancel generation | An in-progress generation is canceled and playback is also stopped. A canceled job is not resumed; the next generation starts as a new job. |
| F-16 | Save audio | The audio is saved as a single WAV file, in the format defined by F-82, at a location chosen by the user. A job that has not completed can also be saved: the segments generated so far are exported, in order, and the file is presented and named as partial so it is never mistaken for the whole document. The user is told how much of the source text the file covers. Exporting does not resume, cancel, or otherwise disturb generation, and exporting a partial result again later produces a longer file rather than a continuation. Retrieval of ready segments through external integrations follows F-55. |
| F-81 | Segment splitting | Segmentation is sentence-first. A sentence longer than 200 code points, or estimated to exceed 20 seconds of audio, is split further at clause boundaries such as commas, semicolons, colons, and coordinating conjunctions, never inside a word, a number, or a grapheme cluster. The first segment of a job is capped at an estimated 8 seconds so that playback can start sooner. A split never produces a segment shorter than 20 code points unless the source sentence is itself shorter. The rules are identical for Korean and English and for every entry path, so the same text always yields the same segmentation. |
| F-82 | Output audio format | Every result is mono 16-bit PCM WAV. The sample rate is the selected model's native rate, is reported by F-53 and recorded in F-84, is shown in the GUI, and is not resampled. All segments of one job and that job's full WAV share one format, and the full WAV is a bit-exact concatenation of its segment audio together with any inter-segment silence attributed by F-08. Results produced by different models may differ in sample rate. |
| F-83 | Auto-play control | Auto-play at the first ready segment can be turned on and off in the GUI. The default is on. With it off, generation proceeds normally, playback is started by the user, and highlighting behaves identically. External requests never auto-play regardless of this setting, per F-51. |

### 2.4 Model Retention and Resource Management

| ID | Feature | Specification and verification criteria |
| --- | --- | --- |
| F-17 | Model reuse | After a job completes normally, the model is kept in memory. If generation is run again with the same model and resource settings, the model is not reloaded. The first generation after restarting the app requires loading again. |
| F-18 | Retention across voice setting changes | Changing only gender, voice, language, speaking style, or tempo keeps the loaded model. |
| F-19 | Model release | The user can explicitly release the model. It is also released when a new model or resource limit is applied to a new job, and on generation cancellation, error, or app exit. The point at which settings changes apply to an in-progress job follows F-78. If only an idle model is released, already-generated audio remains available. |
| F-20 | CPU setting | A target or limit is set within 10–70% of total CPU. The default is 20%; per-OS enforcement levels follow N-03. |
| F-21 | Memory setting | A memory limit is set. The default is based on roughly 25% of total RAM, within a 2–6 GiB range; the user-configurable ceiling is at most 32 GiB and no more than half of total RAM. The value actually applied may be lower depending on currently available memory. |
| F-22 | Usage display | The CPU percentage and RAM usage consumed by the speech generation job are displayed. Usage continues to update while idle with a model retained. |
| F-23 | Out-of-memory handling | If usage exceeds the limit or system free memory becomes insufficient, the job is halted, the model is released, and the reason is reported. If a minimum allowed budget of 2 GiB cannot be secured before starting, the model is not loaded. |
| F-87 | Inference runtime constraints | The inference runtime is restricted to local CPU execution by an explicit allow-list of execution providers, never by relying on a default. Providers that execute remotely, and accelerator providers, are refused even when the installed runtime offers them, which is what makes N-01 and N-05 verifiable rather than incidental. Worker thread counts are set from the CPU budget in F-20 rather than left to the runtime's own defaults. |

### 2.5 Settings and Error Notification

| ID | Feature | Specification and verification criteria |
| --- | --- | --- |
| F-24 | Remember settings | After a normal exit and relaunch, the model, language, gender, voice, speaking style, tempo, CPU, and memory settings are restored. Unsaved input and the previous playback session are not restored automatically. Documents and history the user saved can be reopened from the library. |
| F-25 | Error notification | Failures in opening files, preparing models, generating speech, and so on are reported to the user. Detailed error information is available on generation failure. |
| F-86 | Display language | The application display language can be set to Korean or English, defaulting to the operating system language and falling back to English. It is independent of the narration language in F-05 and of the source text, and changing it never alters saved documents, job snapshots, or API responses. |

### 2.6 Reading Position Highlighting

| ID | Feature | Specification and verification criteria |
| --- | --- | --- |
| F-26 | Bold the current segment | The segment of source text actually being played is shown in bold. Segments are not highlighted merely because they have been generated but not yet played. The default precision is segment level; word-level synchronization is not required. The emphasis is rendered by a metric-stable mechanism so that it does not change text layout, per N-13. |
| F-27 | Linking source text to audio | For each segment, the position in that job's source text is linked to the audio start and end times. The correct position is highlighted even when the same sentence repeats, and for input containing Hangul, Latin text, symbols, line breaks, and emoji, no characters may be dropped or linked to the wrong position. Text normalization performed before synthesis, such as number, date, currency, and abbreviation expansion, must carry an alignment back to the original text, so that every segment range refers to what the user entered and never to normalized text. Emoji and decorative symbols are not sent for synthesis, because a model that receives them vocalises them rather than passing over them; their source ranges are attached to a neighbouring segment so that the highlight still travels across them and the source text is still complete. The same attachment rule covers any span that yields no audio, such as a run of whitespace. Inter-segment silence, which the app inserts itself per F-82, is attributed to the segment that precedes it. |
| F-28 | Highlighting by state | During playback the current segment is highlighted; during pauses between segments the previous segment's highlight is retained. While paused or waiting on the buffer, the last position is retained but the state is indicated separately. On stop, cancel, or natural end, highlighting is cleared. On seek, highlighting is updated to the new position. |
| F-29 | Source text preservation | Bold is for on-screen display only and does not insert asterisks or formatting characters into the source text. Highlight formatting does not leak into copying, database storage, or regeneration. The source text used for generation is fixed per job, and changing the input does not link previous audio to different source text. Because the highlight refers to that frozen snapshot while the input surface stays editable, the two are compared continuously: while the input matches the snapshot the highlight tracks playback, and on any divergence the highlight is cleared and reported as unavailable rather than re-anchored to edited text, as in F-31. If the input matches the snapshot again the highlight resumes, so availability is a function of the current text alone and carries no hidden state. Playback is unaffected either way, because the audio still corresponds to the snapshot. |
| F-30 | Follow the reading position | Auto-scroll that keeps the current segment on screen can be turned on and off. The default is on. If the user scrolls manually, following is paused and resumes via a return-to-current-position action. Highlight changes must not steal the caret, the selection, or keyboard focus. |
| F-31 | Highlighting on history playback | If saved audio along with source text and segment information exists, reopening it highlights identically. If existing history or restored data has no mapping information, playback is allowed but it is indicated that highlighting is unavailable. Precise reading positions are not estimated arbitrarily. Importing external audio files is not included. |

### 2.7 Input Formats and Unsupported Files

| ID | Feature | Specification and verification criteria |
| --- | --- | --- |
| F-32 | Pre-check of supportability | The file extension alone is not trusted; whether the file can be safely read as text is verified. Unsupported, corrupted, encrypted, permission-denied, oversized, and encoding-error cases are distinguished. On failure, the existing input, documents, and playback job are left unchanged. |
| F-33 | Unsupported format notice | The reason the format cannot currently be read, the supported formats, and how to copy the text or convert to TXT are reported. It is not suggested that merely renaming the extension will resolve the issue. No automatic external upload or online conversion is performed. |
| F-34 | Encoding recovery | UTF-8 and CP949 reading are supported first. If automatic interpretation fails or corruption is suspected, an encoding selector and preview are offered and applied only after the user confirms. The source text is never silently corrupted with arbitrary replacement characters. |
| F-35 | Non-standard text confirmation | Files with no extension or an unknown extension may be opened as plain text, after a preview, only if they are determined to be safe text. Binary, executable, and archive files are not force-interpreted as text. |
| F-36 | Input replacement protection | When opening a file would replace unsaved input, user confirmation is required. If the user cancels or validation fails, the existing content is retained. The 50,000-character limit is not circumvented by automatic truncation. |
| F-37 | Identical validation across integration paths | The GUI, REST API, and MCP apply the same input limits and support policy. Non-interactive requests do not arbitrarily choose an encoding or similar; they return a correctable error. |

| Format | Handling policy |
| --- | --- |
| TXT | Directly supported. Applied after encoding validation. |
| Markdown | Directly supported as plain text. Narration optimization for tables, links, and code, and formatting interpretation, are out of scope. |
| No extension / other text | Handled as plain text after confirmation per F-35. |
| PDF / DOC / DOCX / HWP / HWPX / EPUB | Unsupported notice, plus guidance on copying text or converting to TXT. Automatic body-text extraction is excluded from scope. |
| Images / scanned documents | Notice that OCR is unsupported. It is never indicated that text inside images can be read. |
| HTML / web URL | Web page fetching and execution are unsupported. The user may copy the body text into the input. |
| Audio / video / archive / executable files | Text import is refused. Speech recognition, decompression, and execution are not provided. |

When new formats are added later, the text extraction scope, reading order, table and footnote handling, encrypted documents, size and processing time limits, source-loss warnings, and review samples must be defined separately.

### 2.8 Local Database and Library

Database support means local document and job history management usable without installing a separate server. No specific database product or external database connectivity is required.

| ID | Feature | Specification and verification criteria |
| --- | --- | --- |
| F-38 | Document library | Titles and text can be saved, viewed, edited, and deleted, with created and modified timestamps displayed. Automatic body-text saving is off by default; explicit user-initiated saving is always available. |
| F-39 | Generation history | For jobs the user consented to retain, the source-text snapshot, voice settings, model identification, state, generation time, audio length, and whether a result exists are stored. The resource budget actually applied can also be viewed. |
| F-40 | Search and filter | Document titles and retained body text can be searched, and jobs can be filtered by date, model, and completion state. Lists are retrieved page by page, and a list can be displayed without reading entire audio files. |
| F-41 | Reopen and regenerate | A retained job can be opened to review its source text and settings, and if a result exists it can be played and saved. Regeneration is recorded as a new job and does not overwrite existing results. Identical settings do not guarantee completely identical audio. |
| F-42 | Retention policy | One-off jobs and retained history are distinguished. One-off body text and audio are not stored in permanent history. Whether data is stored and when retention expires are clearly conveyed to the GUI and to external requesters. Concrete defaults follow Section 4. |
| F-43 | Deletion and space management | Deletion scope is previewed and confirmed, distinguishing documents, jobs, and audio. Deleting a document alone is never presented as having deleted a separate job's source text. Options to delete related jobs together and to delete all history are provided. WAV files the user exported separately are not deleted. |
| F-44 | Backup and restore | Documents, history, and audio selected by the user are backed up and restored. It is disclosed that backups contain body text and audio. Model weights and credentials are excluded. Corrupted or incompatible backups are rejected and existing data is preserved. Restore adds new items by default, and original identifiers are distinguished from post-restore identifiers. Restored data belongs to the GUI owner, and external client access permissions are not restored automatically. |
| F-45 | Abnormal termination recovery | On relaunch, interrupted jobs are not marked complete but are reconciled to an interrupted state. They are not regenerated or played automatically. If a missing or corrupted result file is detected, the reason playback is unavailable is given along with delete and regenerate actions. |

### 2.9 Common External Integration and Job Management

| ID | Feature | Specification and verification criteria |
| --- | --- | --- |
| F-46 | Integration activation | The local REST service is on by default. It binds the loopback address only, always requires authentication, and can be turned off by the owner. MCP is off by default and is enabled explicitly. Because the MCP server is a REST client per F-58, turning REST off also makes MCP unavailable, and that consequence is stated at the point of the change. The activation state, bind address and port, connection method, and granted permissions can be reviewed. |
| F-47 | Shared features and budget | All entry paths use the same model list, input validation, job states, and resource budget. Across the GUI and all clients combined, concurrent generation is limited to one. A second request is refused with a busy error, and no automatic queue is provided. |
| F-48 | Asynchronous jobs | When a generation request is accepted, a job ID is returned, and progress, errors, and results are checked via separate queries. An accepted job is not canceled, nor is the same job regenerated, merely because the connection dropped. |
| F-49 | Re-requests and cancellation | Duplicate generation is prevented using a retry identifier supplied by the requester. The same identifier with the same content returns the same job, while different content is refused as a conflict. Cancellation is provided as explicit job cancellation, and repeated cancellation adds no further side effects. |
| F-50 | Ownership and permissions | By default a client can query and cancel only its own jobs and results. Library access requires separate authorization. The GUI owner can review and cancel all jobs. External integrations cannot raise the global resource budget or halt other jobs. Requests carry no priority by path: a request from the GUI does not preempt one already running for a client, and the owner reclaims the single slot by cancelling, which F-69 makes reachable in one place. |
| F-51 | Playback of external requests | External requests perform speech generation only and do not auto-play on the host speakers. The user must select and play that job in the GUI. External requesters do not overwrite UI input or take over the user's playback. |
| F-52 | Server lifetime | Integrations are available only while the app is running. The REST service starts with the app unless the owner turned it off; the MCP server is a separate short-lived process started by the MCP client. On exit, active jobs and connections are reported and exit is confirmed; on exit, the servers and model are also cleaned up. Turning off an integration blocks new requests, cancels that integration's in-progress jobs, and then shuts it down. An MCP server process started while the app is not running, while REST is off, or while MCP is disabled reports that condition to its client as a distinct, non-retrying error and never launches the app. GUI jobs are unaffected. |

### 2.10 REST API

| ID | Feature | Specification and verification criteria |
| --- | --- | --- |
| F-53 | Service and capability query | Uptime state, API version, supported models with their native sample rate and minimum resource budget, voices, languages, speaking styles, input formats, and the actual resource policy can be queried. An undownloaded model is distinguished from a server fault. |
| F-54 | Generation request and status query | A job is created from text or a TXT/Markdown upload to be validated, voice settings, and a retention flag. Every job request and every job representation carries an explicit job kind, and this release accepts exactly one value; an unrecognised kind is refused rather than defaulted, so that a client always states what it is asking for. Required values, ranges, and mutual contradictions are validated, and unknown models or options are not arbitrarily substituted. Upload is an alternative to the generation request body and cannot be specified together with inline text. |
| F-55 | Segments and final result | For generated segments, the order, source-text range, start and end times, and retrieval identifier can be queried, and ready audio can be retrieved. After completion, the full WAV is provided in the format defined by F-82. Ungenerated segments and incomplete final files are never returned as if they were finished results. |
| F-56 | History query | Only when separately authorized may a client list and view details of retained jobs it owns. A summary query that excludes body text is the default, and the source-text snapshot of a retained job is retrieved through a separate, separately authorized request. External document editing, deletion, and full-database queries are not supported. |
| F-57 | Errors and documentation | A versioned interface contract, a machine-readable API specification, and request/response examples are provided. Errors include a consistent code, a user message, whether retry is possible, and a request identifier, and do not expose internal paths or credentials. Responses that are retryable in principle, namely busy, rate limited, and insufficient resources, carry a retry-after hint so clients do not have to guess a backoff. |

The external interface contract is as follows. Paths and responses are the public functional contract that clients depend on and do not specify internal implementation structure.

| Method and path | Function | Main results |
| --- | --- | --- |
| GET /api/v1/status | Service, version, supported capabilities, resource policy | 200 |
| GET /api/v1/models | Models and voice/setting choices | 200 |
| POST /api/v1/jobs | Create from text JSON or text file upload, with an explicit job kind | New acceptance 202 + job ID; duplicate re-request 200 + existing ID |
| GET /api/v1/jobs | Authorized retained history query | 200 + page info |
| GET /api/v1/jobs/{id} | State, progress, whether the result is ready | 200 |
| GET /api/v1/jobs/{id}/text | Source-text snapshot of a retained job, separately authorized | 200 |
| POST /api/v1/jobs/{id}/cancel | Cancel job | Cancellation request 202; already-terminated job 200 + final state |
| GET /api/v1/jobs/{id}/segments | List of ready segments | 200 + last sequence number |
| GET /api/v1/jobs/{id}/segments/{segment_id}/audio | Audio of a ready segment | 200 + audio/wav |
| GET /api/v1/jobs/{id}/audio | Completed full audio | 200 + audio/wav |
| GET /api/v1/jobs/{id}/result | Result metadata: format, sample rate, length, size, integrity value, expiry | 200 |

All paths are subject to authentication. Malformed requests are 400, unauthenticated 401, insufficient permission 403, nonexistent jobs or jobs owned by another owner 404, busy/result-not-ready/re-request conflict 409, size exceeded 413, unsupported format 415, invalid voice settings 422, request rate exceeded 429, and insufficient resources to start 503. Even for the same HTTP status, a detailed error code is provided. Responses of 409 for busy, 429, and 503 include a retry-after hint. The service listens on the loopback address only, and a request arriving with an unexpected Host or Origin is rejected before authentication is evaluated. Generation errors occurring after acceptance are surfaced as the job's failure state.

### 2.11 MCP Server

| ID | Feature | Specification and verification criteria |
| --- | --- | --- |
| F-58 | Local MCP connection | The connection method is local stdio, and the MCP server process is started by the MCP client. The MCP server holds no model, no database, and no job state of its own: it is a client of the local REST service in Section 2.10, reaching the app over loopback with a REST credential supplied in its client configuration. Client registration information, including that credential, is provided to the user by the app. Compatible protocol versions and the tool list are stated, and unsupported versions are clearly refused. The revision cited in this section is stateless and has no connection-establishing handshake, so the server implements the capability-discovery call that revision requires, carries its identity and protocol version in each result, and treats every request as self-describing. Streamable HTTP is excluded from scope. |
| F-59 | Tool provision | Querying supported models, requesting generation, querying jobs, canceling, querying segments, and retrieving results are provided as tools. Each tool maps onto one REST operation and adds no capability REST does not already expose. Input and output schemas and side effects are described, and the same policies as F-46 through F-52 apply. |
| F-60 | Result delivery | Instead of long-running tool calls that wait for model loading to finish, a job ID is returned. Audio is described by format, sample rate, size, length, and readiness state, and is delivered as an MCP resource whose URI the client reads over the same stdio session; the MCP server resolves that URI by retrieving the bytes from REST on the client's behalf. Large body text or audio is not embedded in the default tool response. Because the cited revision makes resource reads cacheable, every result reference carries a freshness hint no longer than the remaining result lifetime in Section 4.1 and is marked private, so that no shared intermediary may cache one owner's audio. Cross-call state travels as a server-minted job identifier passed as an ordinary tool argument, which is the pattern that revision prescribes. |
| F-61 | Permissions and consent | When registering a local client, generation, result reading, and retained-history reading are authorized separately and permissions can be revoked. An MCP client's effective permissions are exactly those of the REST credential it was registered with, so expiry and revocation take effect through one mechanism. No tools for arbitrary file read/write, command execution, direct database queries, or server configuration changes are provided. |
| F-62 | Client compatibility | Tool discovery, input errors, job progress queries, cancellation, and result retrieval are verified on the actual target clients. Support for an audio playback UI in the client is not assumed, and a method for retrieving results is provided. Behavior when the app is not running, when REST is off, and when the credential has expired is verified on the same clients. |

| Tool name | Function provided |
| --- | --- |
| list_models | Query models, voices, languages, speaking styles, and input limits |
| create_speech | Accept a job from text and settings. File and URL inputs are not accepted. |
| get_speech_job | Query the state, progress, and errors of one's own job |
| cancel_speech_job | Cancel one's own job |
| list_speech_segments | Query ready segments and their source-text/time mapping |
| get_speech_result | Retrieve a result reference for the completed WAV or a specific ready segment |
| list_speech_history | Query one's own retained history when separately authorized |

MCP result references are MCP resource URIs, readable by an authorized client over the same stdio session. They are opaque identifiers rather than addresses: arbitrary local file paths, and URLs carrying authentication tokens, are never returned to the client. The MCP server requires the REST service to be enabled and resolves every reference against it using its own credential. Access is refused after a reference expires, permissions are revoked, or the data is deleted. Diagnostic output from the MCP server goes to its own error stream rather than through the protocol's logging feature, which the cited revision deprecates. Cancellation of an MCP request itself is distinguished from cancellation of an already-accepted generation job; halting a job is done with cancel_speech_job.

The MCP interface is verified against the tool and transport conventions of the adopted official protocol. The default transport choice and tool composition are the product's connection policy. Reference: [official transport specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports), [official tools specification](https://modelcontextprotocol.io/specification/2026-07-28/server/tools).

### 2.12 Operations Management

| ID | Feature | Specification and verification criteria |
| --- | --- | --- |
| F-63 | Model management screen | Per-model download state, version, disk usage, supported languages, voice and speaking-style controls, license, and whether the model can run are displayed. Before downloading, the required storage space and execution constraints under the current resource budget are reported. |
| F-64 | Download control | Progress, failure, cancellation, and retry of model downloads are provided. On retry, sound already-downloaded data that can be reused is used and corrupted data is re-downloaded. A canceled download is never shown as ready. |
| F-65 | Model verification, repair, deletion | Whether model files are sound can be checked, and a corrupted model can be repaired or its cache deleted. For a model in use, confirmation is required to first cancel the job and release the model. Deleting a model does not delete retained documents or audio results. |
| F-84 | Model manifest | Each supported model is defined by a manifest entry recording its source, pinned revision, file digests and total size, license and usage restrictions, native sample rate, supported languages, and the minimum resource budget under which it is approved to run. Download, verification, and repair in F-63 through F-65 resolve against this manifest, and a model whose files do not match it is reported as corrupted rather than used. Manifest changes ship with the application and are never applied as a silent remote update. |
| F-66 | Voice setting presets | Combinations of model, language, gender, voice, tempo, and speaking style can be saved, applied, renamed, and deleted as named presets. Presets do not change resource ceilings or integration permissions. Unavailable models and voices are flagged with a warning and never arbitrarily substituted. |
| F-67 | Output device and volume | An available audio output device can be selected, and app playback volume and mute can be adjusted. If the output device disappears, playback is paused and it does not switch to another speaker without user confirmation. Playback volume does not alter the signal of the generated WAV. |
| F-68 | Sleep and device-change recovery | On resume from sleep, system resources and the output device are re-checked and playback is left paused. In-progress generation continues where it can be sustained; where it cannot, the reason for interruption is displayed. New jobs are not created automatically. |
| F-69 | Job and service status screen | The current job's request path, owner, and progress stage, the loaded model, the budget actually applied, usage, and REST and MCP state are all viewable together. From this screen the owner can cancel jobs or stop integrations. |
| F-70 | Completion and error notifications | Generation completion, download failure, resource shortage, and backup failure are notified inside the app. OS notifications are provided only if the user turns them on and do not contain body text or audio. Clicking a notification does not auto-play audio. |
| F-71 | Client and credential management | Integration client names, granted capabilities, last access time, and expiry date can be viewed, and permission changes and revocation performed. Because the REST service is on by default, an owner credential is created on first launch and is available in the app for the owner to copy. REST credentials are shown only once at creation and are reissued if lost; the app stores only a verifier from which the credential cannot be recovered, never the credential itself. Expiry and revocation apply immediately to new access and to result retrieval. |
| F-72 | Diagnostic export | The app, OS, and model versions, service state, applied budget, recent error codes, and logs stripped of sensitive information are exported after the user reviews them. Body text, audio, credentials, and the user's home path are excluded, and nothing is transmitted externally automatically. |
| F-73 | Storage management | The sizes of the model cache, documents and history, audio, temporary data, and logs are shown separately. The user can adjust retention limits and clean up expired data. Data in use by current playback, generation, or backup is excluded from cleanup. |
| F-74 | Scheduled backup | A once-daily backup to a local location chosen by the user can be configured. The default is off. It runs only while the app is running and is deferred during generation and restore. A missed schedule is made up only once on the next run and does not accumulate indefinitely. |
| F-75 | Version check and update notice | The installed version and change history can be reviewed, and the released version can be queried at the user's request. Nothing is downloaded, installed, or restarted without user consent. Before an update, active jobs and unsaved input are protected, and data compatibility and recovery information are provided. |
| F-76 | Settings reset and personal data deletion | Resetting voice and display settings, revoking integration permissions, deleting retained data, and deleting the model cache are offered separately. Full deletion proceeds only after confirming the scope of impact and what cannot be recovered. Failed items are reported, and external backups and exported files are not deleted automatically. |
| F-77 | Unsaved input and exit protection | When replacing input or closing the window, save, discard, and cancel are offered for unsaved text. If a job or external connection is active, that is reported as well. On relaunch, audio never starts automatically and no generation job is resumed. The REST service starts with the app according to the owner's retained setting, which is on unless the owner turned it off. |
| F-78 | Resource policy changes | The displayed setting value is distinguished from the value applied to the current job. A change made during a job applies to the next job; if immediate application is chosen, cancellation of the current job is confirmed. External clients cannot change the limits set by the owner. |
| F-79 | Service conflict notices | REST port occupancy, insufficient access permissions, MCP connection failure, and duplicate instances are reported distinctly. A failure to bind the REST port disables the integrations only: the GUI, generation, playback, and the library remain fully usable, and the condition is shown as an actionable notice rather than a startup failure. On a port conflict, other programs are not terminated, no alternative port is bound silently, and the interface is not automatically exposed externally. The owner can choose a different local port and restart the service without restarting the app. |
| F-85 | Single instance | Only one instance of the application runs per user account. A second launch surfaces the existing window instead of starting a second engine, database session, or REST listener, and never creates a second generation slot. |
| F-80 | Help and policy review | Supported formats and per-model constraints, storage locations and retention policy, the meaning of resource limits, integration settings, personal data handling, licenses, and version information can be reviewed inside the app. Where a model license restricts what the user may do with it or with its output, those restrictions are presented as terms the user accepts before that model is first prepared, not merely filed among the notices, per N-11. Basic help is readable offline. |

## 3. Non-Functional Specification

| ID | Quality attribute | Specification and constraints |
| --- | --- | --- |
| N-01 | Local processing and privacy | Conversion of input text to speech is performed on the user's PC and is not sent to an external speech API. Generating speech with a prepared model does not require an internet connection. No automatic external communication occurs other than model downloads and update checks requested by the user. The local REST service is not external communication: it binds the loopback address only and is never reachable from the LAN or the internet, per N-17. |
| N-02 | Data retention | Models, settings, and documents and history saved by the user are kept locally. One-off input and audio are cleaned up according to the lifetimes in Section 4, and temporary data left by a forced termination is cleaned up on relaunch. Retained data and WAV files exported by the user are never deleted automatically. |
| N-03 | Host resource protection | On Windows, enforced limits are applied to the CPU and memory of the speech generation job. On macOS, CPU throttling and memory monitoring are provided, but an enforced ceiling that blocks even momentary usage spikes is not guaranteed. The RAM usage shown on screen and the memory value used for limit decisions may differ. |
| N-04 | Reserving system headroom | The budget is adjusted so that the greater of 10% of total RAM or 1 GiB remains free for the system, monitored at roughly 0.5-second intervals. Momentary memory shortages between monitoring intervals, and all host load, are not guaranteed to be prevented. |
| N-05 | Laptop suitability | The app must be able to run on CPU without a discrete GPU and does not automatically occupy the GPU, which F-87 makes explicit rather than incidental. Because required memory and speed differ by model, a model may be unable to run under a low limit, and F-04 requires saying so rather than failing at generation time. |
| N-06 | Responsiveness | Playback control and cancellation must remain usable during generation. Model reuse skips repeated loading, but time-to-first-audio and time-to-completion vary with the model, input, and resource budget. Response criteria for external control requests follow N-22. |
| N-07 | Playback latency | Playback starts before full completion once the first portion is ready. Waiting for initial model loading and generation of the first portion is unavoidable. Faster-than-real-time generation and gapless playback are not guaranteed on all PCs and models. |
| N-08 | Voice quality | The goal is Korean and English pronunciation with natural narration. Naturalness, emotional expression, and mixed-language pronunciation vary with the model, voice, and sentence, and no fixed quality score is guaranteed. Before release, Korean and English review sentences are evaluated for omissions, repetitions, pronunciation, and whether settings are reflected, and per-model characteristics are documented. |
| N-09 | Usability | Input, voice settings, resource settings, generation, and playback must be reachable from the main screen. The current state and selected values are displayed identifiably, and unavailable controls are disabled. |
| N-10 | Distribution and compatibility | Windows is distributed as a folder containing the EXE and companion files; macOS is distributed as an app. The user must not need to separately install Python, Node.js, or a database server. Supported environments and release approval criteria follow Section 8. |
| N-11 | Usage rights | The license and usage restrictions applicable to each model are honored. The fact that a model can be used locally does not imply unlimited use for any purpose or the right to redistribute. Where a model's license obliges a distributor to impose the same use restrictions on its own users, as the responsible-AI licenses in this class do, the product's terms must carry those restrictions through to the end user, and shipping such a model without that clause is a release blocker. Per-model license obligations are recorded in the manifest in F-84 and surfaced per F-63 and F-80. |

### 3.1 Synchronization, Data, and Integration Quality

| ID | Quality attribute | Requirements and review criteria |
| --- | --- | --- |
| N-12 | Highlight synchronization | At segment boundaries, the difference between the playback position and the on-screen highlight transition must be within 300 ms in the reference environment of Section 8. Output device latency is measured and recorded separately. No claim of word-level accuracy is made. |
| N-13 | Layout stability | Bold transitions must not cause line breaks and on-screen positions to shift repeatedly. Because a heavier weight normally changes glyph advance widths, the emphasis must be rendered by a metric-stable mechanism, such as a variable font whose weight axis preserves metrics, or a layout measured at the emphasized weight for all text. Basic input, generation, cancellation, playback, and follow controls must be operable by keyboard alone, and the current reading position must not be distinguished by color alone. |
| N-14 | Data integrity | Document edits are separated from job snapshots, and previously sound data is preserved even if a save fails midway. Mismatches between audio and history are detected. On database locks or insufficient space, the system does not wait indefinitely or report success. |
| N-15 | Updates and recovery | A recoverable backup is secured before any data format change. When an older app opens an incompatible new data format, it does not modify it destructively. When restoring from corruption, existing data is validated before being overwritten. |
| N-16 | Storage limits | The retention space ceiling and remaining space are displayed, and explicitly saved documents and retained results are never silently deleted to free space. When storage is insufficient, retention requests are reported as failures. Deletion failures are shown as retryable and are not mistaken for completed deletions. |
| N-17 | Local service security | REST is exposed only on the loopback address and always requires authentication, including on a first launch where it is on by default. Binding to any non-loopback address is not configurable. CORS is disallowed by default, and allowed Host and Origin values are validated before authentication is evaluated. Tokens can be reissued and revoked, are stored only as verifiers, and are never included in URLs, logs, or backups. LAN and internet exposure are not supported. |
| N-18 | Input and file security | Text is never executed as commands, HTML, or MCP instructions. Arbitrary paths and external URLs from external requests are not read, and results are not written to arbitrary paths. Instructions contained in filenames and document body text are treated as data. Where a model accepts an expressive instruction, per F-08, that instruction travels in a channel structurally separate from the source text, so text supplied by a user or a client can never alter the speaking style, the voice, or the model's behavior. |
| N-19 | Permission boundaries | Knowing a job ID or result reference must not by itself grant access. Ownership and permission revocation across the GUI, REST, and MCP are verified. This is a local single-user app, and isolation against an attacker who has fully compromised the same OS account is not guaranteed. |
| N-20 | Data minimization | Permanent retention of body text and audio is subject to explicit consent, and default logs contain no body text, audio, or tokens. It is disclosed that the app cannot control what an integration client does with text and audio it has received, including transmitting it externally. |
| N-21 | Unified resource budget | Budget is not allocated redundantly to the GUI, REST, and MCP. The CPU and memory limits of the generation job are measured separately from total app usage including the UI, database, and servers. List queries and audio retrieval must not lead to unbounded in-memory loading. |
| N-22 | Integration responsiveness | Request acceptance, queries, and cancellation must respond independently of model loading and generation. In the reference environment of Section 8, request acceptance, query, and cancellation responses must be within 1 second at p95, and resource release for a generation job after cancellation is accepted must be within 5 seconds. Model download and generation times and large backup times are measured separately. |
| N-23 | Overload defense | Limits are applied to request body size, request rate, active jobs, result reference lifetime, and retention capacity. When a limit is exceeded, the reason is reported immediately and no unbounded queue is created. Dropped connections and repeated requests must not cause jobs or models to multiply. |
| N-24 | Interface consistency | The GUI, REST, and MCP share the same job states and error semantics. The REST contract in Section 2.10 is the canonical external contract and MCP is a projection of it that adds no capability of its own, so a behavior difference between the two is a defect. Changes that break interface compatibility are versioned distinctly, and supported versions and change history are provided. |
| N-25 | Diagnosability | Problems must be traceable via job ID, timestamp, stage, error code, and applied budget. Diagnostic information is exported without sensitive input, and log retention periods are limited. |

### 3.2 Operational Stability

| ID | Quality attribute | Requirements and review criteria |
| --- | --- | --- |
| N-26 | Long-run stability | In the reference environment, repeating generation, queries, playback, and cancellation with the same model for 8 hours must produce no duplicate jobs, no unresponsiveness, and no unmanaged growth in files or memory. Resources of terminated jobs are reclaimed per the retention policy, and resident model memory is measured separately. |
| N-27 | Backup safety | A backup must be a consistent data bundle, and integrity and compatibility are verified before restore. Paths and links inside a backup are prevented from overwriting files outside the restore target. Not only compressed size but also the space and item count required after restore are limited. |
| N-28 | Background task priority | Downloads, verification, scheduled backups, and cleanup yield to the user's generation and playback. Scheduled backups and model verification are deferred during generation, and scheduled tasks never wake the system from sleep or launch the app automatically. |
| N-29 | Distribution trustworthiness | The origin and integrity of official distribution files must be verifiable. Code signing is applied to Windows distributions and signing plus notarization to macOS distributions, and disabling security features is never required as part of installation. Unverified updates are not applied automatically. |
| N-30 | Accessibility and display adaptation | Basic features must be usable by keyboard, and icons must have accessible names and descriptions. On screens of 1280×720 or larger with OS scaling of 100–200%, main features must remain reachable via scrolling or layout changes, and text and controls must not overlap or be clipped. |
| N-31 | Default-on service safety | Because the REST service listens from first launch, it must fail closed: no request succeeds without a valid credential, no credential is shared between clients, and no default or well-known credential exists in a distribution. Its presence, bind address, port, and the way to turn it off are disclosed on first launch and reviewable per F-80. A bind failure degrades integrations only and never blocks local use, per F-79. |

## 4. Operating Policies and Data Scope

The following defaults apply on first launch. Changeable values are managed in settings and never take precedence over safety limits or per-OS constraints.

### 4.1 Default Policies

| Item | Policy |
| --- | --- |
| Reading highlight | Segment-level bold. Follow is on by default. |
| Input limits | 50,000 characters; imported files 2,000,000 bytes. REST request bodies are also capped at 2,000,000 bytes, including upload metadata. |
| Concurrent generation | One for the whole app. No separate queue. |
| Model retention | One model at a time. Released on model switch, policy change, cancellation, error, exit, and memory protection. Requests from other clients reuse the model if it is the same one. |
| Permanent storage | Automatic document saving and job history retention are off by default. Data is stored on manual save or on a per-job retention request. |
| One-off results | In the GUI, a result is cleaned up when it is replaced or when the app exits normally. External jobs remain retrievable until 1 hour after reaching a terminal state, or app exit, whichever comes first. |
| Re-request identifiers | Scoped per client. Kept for at least 1 hour during job execution and after termination. Duplicate-prevention information is retained across a normal restart for that period, without retaining one-off body text or audio. It is disclosed that reusing the same key after expiry may become a new request. |
| Retention space | 5 GB by default for the combined app-managed documents, history, and audio. Model cache, user backups, and exported WAV files are shown separately. Retained data is kept until the user deletes it, and new retention is refused when the limit is reached. If the limit is reached at runtime, jobs requesting retention fail with a storage error. |
| Logs | Diagnostic logs without body text for 7 days, up to 100 MB. Old logs are cleaned up according to whichever limit is reached first. |
| External connections | REST on by default, loopback only, authentication always required. MCP off by default, stdio only, and implemented as a REST client, so it is unavailable while REST is off. No independent background service, and nothing listens once the app has exited. |
| Request rate | Per client, 10 generation requests per minute and 120 other requests per minute. Authentication failures are limited separately. The concurrent-generation limit always applies separately. |
| List queries | 20 items by default, up to 100 per response. |

The following additional defaults and change rules apply to operational settings.

| Item | Policy |
| --- | --- |
| Voice presets | Up to 100. On a duplicate name, overwrite is confirmed. |
| Playback volume | App volume 100% by default, mute off. Playback starts on the system default output device, and device changes during playback are handled per F-67. |
| OS notifications | Off by default. |
| External model downloads | Prohibited by default. Possible only where the owner has permitted it per model. |
| Credential expiry | The owner credential is created on first launch and follows the same policy as any other. REST credentials 90 days by default. The owner can set 1–365 days. A notice appears in the app 7 days before expiry. Expiry does not automatically cancel existing jobs but does block result access; access is possible again with reissued credentials for the same client. Explicit permission revocation also cancels in-progress jobs. |
| REST port | 8765 on the loopback address by default, changeable. On a conflict the service is reported unavailable, the app keeps running per F-79, and the service is retried after the port is changed. No alternative port is chosen automatically. |
| Integration restart | The enablement setting is remembered and reapplied on relaunch, so the REST service starts with the app unless the owner turned it off. Re-enabling a service the owner had turned off is always an explicit action. |
| Scheduled backup | Off by default; once daily when enabled. The 7 most recent sound scheduled backups created by the app are kept. Manual backups are never deleted automatically. Older scheduled backups are cleaned up only after a new backup is verified. |
| Retention capacity changes | Minimum 1 GB, maximum 100 GB. If lowered below the amount already stored, existing data is kept and new retention is blocked. |
| Low-space warning | If free space on the storage device holding app data falls below 1 GB, a warning is shown and downloads, backups, and new generation are not started. If space runs short mid-job, the job fails safely. |
| Authentication failure limits | If REST authentication fails 10 times within 1 minute, authentication retries from that connection origin are blocked for 60 seconds. The local service as a whole and the GUI are not terminated. |
| Backup restore limits | After decompression, at most 100 GB and 100,000 items. The configured retention limit and actual free space must also be satisfied. Data exceeding these limits is rejected before being applied. |

When displaying usage, memory is expressed in GiB (2^30 bytes), storage in GB (10^9 bytes), and transfer limits in bytes. CPU percentage is displayed with total logical CPU capacity as 100%.

One-off mode also permits temporary disk storage as needed during execution. Deletion means removing the app's access path and cleaning up managed files; it does not guarantee unrecoverable secure erasure from the storage device. Database and backup encryption and app password locking are excluded from scope, so local storage alone must never be described as encrypted storage.

### 4.2 Managed Information

| Subject | Information needed for user features | Lifetime and protection |
| --- | --- | --- |
| Document | ID, title, body, created and modified times, version | Kept after explicit save. Editing does not change the source text of existing jobs. |
| Job | ID, kind, request path and owner, state, source-text snapshot, voice settings, applied budget, start and end times, error code, retention flag | Per one-off and retention policy. Must not be exposed to another owner. |
| Segment | Segment ID and order, source-text start and end, audio start and end, readiness | Kept and deleted together with the result. Text ranges are standardized as Unicode code points, start-inclusive and end-exclusive, and are not conflated with UI indices. Times are in milliseconds relative to the start of the audio. |
| Result | Identifier, format, length, size, integrity verification data, expiry time | Queried by a permission-checkable identifier rather than an arbitrary path. |
| Integration permission | Client identifier, granted capabilities, active/revoked state | Separated from body-text history. Credentials are excluded from backups and logs. |
| Re-request record | Client and key, request-match discriminator, job ID, expiry time | Only the information needed for duplicate prevention is kept, with no source text. |

Even if a result has already expired and no longer exists, calling again with a valid re-request key returns the existing job and the result's expired state, and does not regenerate automatically. New generation must be requested with a new key. The same rule applies to a job that reached Failed, Canceled, or Interrupted: the existing terminal job and its reason are returned, nothing is retried automatically, and a new key is required to generate again.

## 5. States and Exception Handling

### 5.1 Job States

- Normal flow: Accepted -> Preparing model -> Generating -> Complete.
- If an error occurs during the execution stage, the job transitions to Failed; explicit cancellation transitions Canceling -> Canceled.
- Non-terminal jobs left after an abnormal termination transition to Interrupted on relaunch.
- Complete, Failed, Canceled, and Interrupted are terminal states, and the same job is never run again. A retry is a new job.
- If cancellation and completion race, whichever terminal state is confirmed first is kept. Completion is never overwritten by Canceled.
- Complete means the full audio is ready and any requested retention also succeeded. If generation finishes but saving fails, the job is not marked Complete.

### 5.2 Playback States

- Not ready for playback, Playing, Paused, Waiting for next segment, Stopped, and Playback ended are distinguished.
- GUI generation auto-plays when the first segment is ready unless auto-play is turned off per F-83. API and MCP generation never auto-play.
- Stopping or pausing playback does not change the generation state. Canceling generation also stops playback of that job.
- When input is replaced or a different history item is opened, existing playback is stopped and switched to the correct job so that existing audio is never played over different source text.
- Editing the input without replacing it does not stop playback. Once the input no longer matches the job's snapshot the highlight is dropped and reported as unavailable, per F-29, and it returns only if the text matches the snapshot again.

### 5.3 Key Exceptions

| Situation | Required behavior |
| --- | --- |
| Unsupported file, corruption, encoding error | Abort before applying, preserve existing input, and provide a specific reason and possible remedies. |
| Model not downloaded for an external request | Only models pre-authorized in the GUI may be downloaded. Otherwise, refuse with MODEL_NOT_READY; arbitrary large downloads are prohibited. |
| Network outage | Display the reason model preparation failed. Generation with fully downloaded local models remains possible. |
| REST service cannot start | Report the reason, keep the GUI, generation, playback, and library fully usable, and mark the integrations unavailable. MCP requests fail with a clear reason because MCP depends on REST. No alternative port or address is bound automatically. |
| Out of memory, failure to apply resource limits | Refuse to start or fail the in-progress job, release the model, and indicate whether retry is possible. |
| Database unavailable, locked, or storage full | Report the save failure and preserve previously sound data. A retention request is never silently converted to one-off. Converting to one-off in the GUI is possible only after user confirmation. |
| Result file missing or expired | Display the reason the result cannot be retrieved. If the history entry itself remains, viewing body text and settings and starting a new generation remain possible. |
| Connection dropped | Keep the accepted job. After reconnecting, the client can query its own jobs. Server shutdown and permission revocation follow separate policies. |
| Client permission revoked | Block new calls and result access, and cancel that client's in-progress jobs. Other users' jobs are kept. |
| Running job present during deletion | Require cancellation first. Data of a running job is never partially deleted. |
| Document changed during backup | Include only data from a consistent point in time. Temporary results of in-progress jobs are excluded from backups. During restore, new generation and edits are blocked and progress state is provided. |

## 6. Acceptance Criteria

The scenarios below are mandatory acceptance items required for release approval. Test reports record the target version, environment, inputs, expected results, actual results, and verdict.

| ID | Related requirements | Scenario and pass criteria |
| --- | --- | --- |
| A-01 | F-01–F-09, F-32–F-37 | Verify empty input, 50,000 and 50,001 characters, file size boundaries, and UTF-8, CP949, and corrupted encodings. On failure or cancellation, the existing input must be unchanged. |
| A-02 | F-26–F-29, F-81, N-12, N-13 | For Korean, English, and mixed sentences, repeated sentences, emoji, and long-sentence splitting, the played segment and the bold highlight must match, and the source text and copied output must be unchanged. Text containing numbers, dates, currency, and abbreviations must highlight the original characters rather than their expanded reading, and emphasis transitions must not shift layout. Emoji and decorative symbols must not be read aloud, must not be removed from the source text, and must not break the highlight as it passes over them. |
| A-03 | F-12–F-15, F-28–F-30 | With generation slower than playback, and across pause, seek forward and back, stop, cancel, and play-to-end, states and highlighting must transition as defined. |
| A-04 | F-29–F-31, N-13 | Focus must not be stolen during manual scrolling or selection, and on source-text changes or history switching, audio must not be linked to a different job. Editing the input during playback must clear the highlight and report it as unavailable rather than shifting it onto the wrong characters, and restoring the text must bring it back. |
| A-05 | F-32–F-37 | Safely reject PDF, DOCX, HWP, image, and executable files as well as samples with disguised extensions. There must be no external transmission, file execution, or replacement of the source text. |
| A-06 | F-24, F-38–F-42 | After saving and restarting, documents and history can be searched, voice settings are restored, and playback with highlighting works. Document edits and regeneration must not alter previous jobs. |
| A-07 | F-42–F-45, N-14–N-16 | Across one-off expiry, retention limits, deletion failure, missing files, corrupted backups, and errors during restore, there must be no loss of explicitly retained data and no false completion. |
| A-08 | F-45, F-48–F-49 | After a forced termination during generation, the interrupted state is shown. No automatic generation or playback. A valid re-request key must not create a duplicate job. |
| A-09 | F-17–F-23, F-47, N-03, N-04, N-21 | Even with simultaneous GUI, REST, and MCP requests, a single generation and the shared budget are maintained. The next request for the same model must not reload it, and on resource shortage the job must halt safely. |
| A-10 | F-46, F-50–F-52, N-17–N-19, N-31 | REST reachable on loopback only and never from another host, MCP off until enabled, and blocking of unauthenticated requests, authentication failures, revoked permissions, access to another job ID, arbitrary paths, and invalid Host and Origin values. External generation must not auto-play on the speakers. |
| A-11 | F-48–F-57 | Verify REST acceptance, status, ready segments, full WAV, cancellation, error codes, and paged queries. A duplicate key must return the same job, and different content must be a conflict. Retryable responses must carry a retry-after hint, and source-text and result-metadata requests must respect their separate authorization. A request with a missing or unrecognised job kind must be refused rather than defaulted, and every job representation must state its kind. |
| A-12 | F-58–F-62, N-24 | On a real MCP client, tool discovery through generation, querying, result retrieval, and cancellation all succeed, with results delivered as MCP resources over stdio. With the app not running, with REST turned off, or with an expired credential, the MCP server must fail with a distinct reason and must not launch the app. Unsupported versions and invalid input are clearly refused, and MCP must expose no capability REST does not. Capability discovery must answer without a prior handshake, and result references must carry a freshness hint within the remaining result lifetime and be marked private. |
| A-13 | N-20, N-25 | Default logs and backups must contain no credentials, and logs must contain no input source text. Verify whether one-off body text and audio remain after a normal exit with retention turned off. |
| A-14 | N-06, N-07, N-12, N-21–N-23 | In the reference environment of Section 8, measure highlight latency by the method in Section 8.2, along with API p95 response, cancellation time, time to first audio, and whole-app versus generation-job CPU, RAM, and disk separately. Shortfalls against targets must be recorded, not hidden. |
| A-15 | N-08, N-10, F-04 | On per-OS Windows and macOS install environments, verify launch, Korean and English generation, playback, database, REST, and MCP. Record the shipped model's minimum execution budget and any unsupported combinations, in the form Section 8.3 requires of every model. |
| A-16 | F-63–F-65, N-28 | Verify download cancellation, corruption, retry, repair, and deletion; deleting a model in use must not proceed without explicit cancellation. Documents and audio results must be preserved. |
| A-17 | F-66–F-68, F-77, N-30 | Verify preset validity, volume, output device removal, resume from sleep, and canceling an exit with unsaved input. There must be no unintended speaker output, loss of body text, or focus stealing. |
| A-18 | F-69–F-72, F-78–F-80 | Job origin, applied budget, and service errors must be clearly distinguished. Verify credential expiry, renewal, and revocation and budget changes, and diagnostic exports must contain no sensitive information. |
| A-19 | F-73–F-76, N-27–N-29 | Verify low space, scheduled-backup deferral, backup rotation, malicious restore paths, update refusal, and selective deletion. The user's manual backups and exported files must be unaffected. |
| A-20 | N-26, N-21–N-23 | Verify over 8 hours with 100 or more warm-state jobs, including cancellations, failures, and API queries. Single generation, limit compliance, normal responsiveness, and policy-based cleanup of temporary data must be maintained. |
| A-21 | F-10, F-11, F-16, F-82, F-83 | Verify the preparation, loading, generating, complete, and error stages and their progress display; that with auto-play off generation, highlighting, and manual playback all remain correct; and that the saved WAV matches F-82 and is a bit-exact concatenation of that job's segment audio. Exporting a job that is still generating, and one that was canceled, must produce a playable file of exactly the segments then ready, labelled partial and reporting its coverage, and must not disturb the job. |
| A-22 | F-24, F-25, F-86, N-09 | After a normal exit and relaunch, every remembered setting is restored while unsaved input and the previous playback session are not. File, model, and generation failures each report a distinguishable reason. Switching the display language leaves documents, job snapshots, and API responses unchanged. |
| A-23 | N-01, N-02, N-05, N-11, F-84, F-87 | With the network disabled, generation with a prepared model succeeds end to end. With the network enabled, no outbound connection occurs other than a user-initiated model download or version check. Model files are verified against the manifest, a tampered file is reported as corrupted rather than used, and each model's license and restrictions are shown before download. Generation must not require a discrete GPU, and the runtime must refuse remote and accelerator execution providers even when the installed build offers them. Where a model license requires it, the use restrictions must be presented as accepted terms before first preparation. |
| A-24 | F-46, F-79, F-85, N-31 | On a first launch, REST is reachable on loopback with the issued credential, rejects every request without one, and is refused from another host. With the port already occupied, the app still launches, generates, and plays, and only the integrations are marked unavailable. A second launch surfaces the existing window rather than starting a second listener or engine. |

Performance review is conducted under the conditions in Section 8, separating cold and warm starts. Response times are measured at least 100 times with the model already prepared, and p95 is computed. Model download, initial loading, and actual generation times are each recorded separately.

## 7. Out of Scope

- User voice recording, voice cloning, and custom voice training
- Saving in formats other than WAV, such as MP3
- Queues of multiple jobs, simultaneous generation with multiple models, and batch processing
- Resuming generation from the incomplete segments of a canceled or interrupted job
- Automatic body-text extraction from PDF, Word, HWP, and EPUB; OCR; and narration based on Markdown formatting
- Precise word-level or phoneme-level timestamps and highlighting
- External database connections, cloud sync, and multi-user accounts for organizations
- REST exposed to the internet or LAN, MCP Streamable HTTP, and remote administration
- An always-on service that keeps the model in memory after the app exits
- Guarantees of real-time generation, uninterrupted playback, and fixed quality on all hardware

## 8. Supported Environments and Release Approval

### 8.1 Supported Environments

| Item | Requirement |
| --- | --- |
| Windows | Windows 11 x64. Runs with standard user privileges and does not require administrator privileges on an ongoing basis. |
| macOS | macOS 14 or later, Apple Silicon. The supported OS list and architecture of the release are stated. |
| Minimum memory | 8 GiB system RAM. Execution is permitted after checking actual free memory and the model budget. Meeting the minimum does not guarantee that every model will run. |
| Recommended memory | 16 GiB system RAM or more. Large models require an additional memory budget. |
| CPU | 4 or more logical CPUs. A discrete GPU is not required. |
| Storage | Space is reserved separately for app installation, selected models, the retention limit, and job temporary space. Required capacity is displayed before installation and downloads. |
| Network | Required for the initial model download and user-requested update checks. Not required for generation with a prepared model, the database, or local integrations. |
| Audio output | An output device available to the OS. Speech generation, saving, and external result retrieval work even with no output device. |
| MCP compatibility | The protocol versions supported by the release and the list of reviewed clients and versions are provided. Compatibility with clients not listed is not guaranteed. |

The minimum specification is an entry condition for running the app, not a guarantee of per-model quality or real-time generation. Support status is re-checked after OS updates, and known compatibility issues along with user workarounds are included in the release notes.

### 8.2 Performance Test Conditions

- Use environments with 16 GiB or more RAM, 8 or more logical CPUs, and an SSD, on both Windows and macOS. Test under the Windows default power mode and macOS default power settings, and record the device model and OS version.
- Use the default Supertonic model, a CPU setting of 20%, a memory budget of 4 GiB, and an already-prepared local model as the baseline. Tests where the actually applied budget was lowered are reported as separate results.
- Responsiveness testing is performed with one generation, integration queries at once per second, and 10,000 or fewer document and job list entries. Speech generation speed is measured separately per model.
- Highlight synchronization is measured against the default wired or built-in output device, with additional-latency devices such as Bluetooth recorded separately. It is measured by capturing rendered audio and the screen on one clock and comparing each segment's audio onset with the frame in which its emphasis appears, over at least 100 segment transitions per model, reporting the median and the 95th percentile. An instrumented build that timestamps audio-clock and emphasis-transition events may be substituted only after it is shown to agree with the capture method.
- Per model, review 20 Korean and 20 English sentences plus samples with mixed languages, numbers, dates, currency, abbreviations, symbols, and repeated sentences. Missing speech, unnecessary repetition, corrupted audio, and clearly incorrect sentence output are treated as release-blocking defects.
- Long-run testing completes 8 hours and 100 or more runs using short inputs, and records the trend of CPU, RAM, open files, and temporary capacity in the same idle state. The cost of first model preparation is separated from normal growth in retained data.

### 8.3 Release Approval Conditions

- All functional requirements and acceptance scenarios must be satisfied. The product is not released if there is reproducible data loss, permission bypass, unbounded duplicate generation, or a mismatch between source text and audio.
- Per-OS installation, launch, normal exit, forced-termination recovery, offline generation, library restore, and REST and MCP result retrieval are reviewed.
- The memory budget under which each shipped model can run, and its limitations, are included in the release materials, and a model that cannot run within the limits is identified as such rather than offered.
- Updates that use existing retained data and backup restores are verified, and destructive changes to unsupported data formats are blocked.
- The version, signature, and integrity of distribution files, license notices, change history, known limitations, and recovery procedures are provided.

## 9. Operating Procedures

| Situation | User procedure and system behavior |
| --- | --- |
| First use | Review storage and privacy policy, including that the local REST service is on and listening on loopback and how to turn it off -> check models and download size -> prepare locally -> enter text -> generate and play. Retention, MCP, and issuing client credentials are the user's choice. |
| Everyday use | Enter text or select a retained document -> check the voice preset -> start reading -> check the bold position and control playback -> retain or export WAV as needed. |
| Registering an external integration | Owner confirms REST is on, and enables MCP if the client needs it -> issue a credential for that client with least privilege -> hand the client its connection details and credential -> authenticated requests -> retrieve status and results. |
| Changing model or resources | Check active jobs -> choose apply-to-next-job or cancel-and-apply-immediately -> re-confirm the model and the actual budget. |
| Insufficient storage | Check usage -> clean up expired temporary data or back up and selectively delete retained data -> confirm free space and retry. |
| Generation failure | Check the failing stage and error code -> check model state, resource budget, and free space -> repair the model if needed -> retry as a new job. |
| Database or backup failure | Preserve the original data and display the error -> select a verified backup -> preview the restore -> restore -> confirm search and playback. Corrupted data is never overwritten automatically. |
| Connection failure | Check that the app is running, then REST availability and port, MCP enablement, credential expiry, permissions, and protocol version -> correct settings -> reconnect. Because MCP depends on REST, check REST first. A connection failure alone never causes duplicate generation. |
| Personal data cleanup | Confirm the deletion scope for documents, related jobs, audio, and client permissions -> delete -> check failed items. Backups and exported files outside the app are to be managed separately, as advised. |
| App exit | Check unsaved input, active jobs, and connections -> choose to save or cancel -> shut down servers, release the model, and clean up one-off data. |

## 10. Requirements Management

- Functional requirements are identified with F, non-functional requirements with N, and acceptance scenarios with A.
- When behavior, limits, retention, permissions, or external interfaces change, the related acceptance criteria and operating guidance are updated along with them.
- Identifiers are allocated sequentially when a requirement is created and are never reused or renumbered, so numbering is not positional: a requirement added later to an earlier section keeps the higher number.
- Every functional and non-functional requirement must be referenced by at least one acceptance scenario. A requirement with no scenario is a defect in this document.
- Whether a feature is provided is governed by the scope of this document; release quality is managed in a separate test report.
- Changes that break compatibility between versions must state their impact on user data and external clients.
- Appendix A is an internal planning note and is not part of the requirements baseline. Nothing in it may appear in the README, release notes, in-app help, marketing material, or the public interface contract until it has been promoted into the body of this document.

## Appendix A. Internal Planning Note

This appendix is not part of the requirements baseline and is not a commitment. It records direction the team needs while building the MVP. Nothing here may be published in the README, release notes, in-app help, marketing material, or the public interface contract until it has been promoted into the body of this document.

### A.1 Scope Beyond the MVP

This release is speech generation only, with one model. Two things are deliberately deferred. Sections 2.7 and 7 state the shipped scope accurately, and no requirement, endpoint, tool, setting, or user-visible string in the MVP may hint at either of them.

**Speech recognition.** Planned as a second capability. It is cheap to add in the engine and expensive in the interfaces, because once a client depends on a path or a tool name, changing it is a breaking change. The contract decision is settled in A.3: a job kind discriminator ships from the start, so a second kind is an additive change. What remains to settle before the first release publishes anything:

| Item | Decision to settle before the interface contract is published |
| --- | --- |
| MCP tool names | The tool names are qualified with `speech`, which reads as generation. `create_speech` and `get_speech_job` would sit awkwardly beside recognition tools, and renaming published tools breaks every client configuration that referenced them. Decide the naming convention once, before the first release, not after. |
| Job model | The Job, Segment, and Result entities in Section 4.2 are already direction-neutral: a segment linking a text range to an audio time range describes recognition as well as generation. Keep it that way, and do not add generation-only fields to Segment. |
| Single generation slot | F-47 allows one job across the whole app. Recognition would contend for the same slot and the same budget. That is probably still the right policy, but it should be revisited deliberately rather than inherited by accident. |
| Terminology | The body says "speech generation" throughout rather than "the engine" or "the model". Keeping that specific makes the later split cheaper than a vague term would. |

**The Qwen3-TTS checkpoints.** Cut from F-04 on the measurements in A.5. The 1.7B needs about 8 GB, which is impossible on Section 8.1's 8 GiB minimum and sits exactly at F-21's ceiling of half of total RAM on a 16 GiB machine; the 0.6B needs about 3 GB, above F-23's 2 GiB floor. Independently of memory, neither has a first-party CPU inference path on Windows: the official package requires Apple's MLX, the fastest third-party engine builds only for WSL2, and the ONNX conversions are unaffiliated uploads with negligible download counts, one of them 12.87 GiB on disk. Both are Apache-2.0, a better licence position than the shipped model has, so they stay attractive. Reconsider when an official CPU export exists, or when the project is willing to own a conversion and its validation. F-04, F-63, and F-84 keep the selection machinery, so restoring a second model is data rather than structure. Until then nothing in the product mentions one.

No recognition work, no additional model selection, and no further model licensing review is in the MVP.

### A.2 Implementation Direction

Non-normative. The body of this document stays technology-independent; this records current intent so it is not re-argued at each review.

- The local REST service is the core. It owns the job engine, the single generation slot, the resource budget, and the database, and it is the only component that talks to a model. It is on by default because it is the product's own service boundary, not an optional add-on.
- The MCP server is a thin stdio process that is a REST client, per F-58. FastMCP is the current candidate. It should hold no job state, no model, and no policy of its own: each tool is one REST call, and its permissions are the permissions of its REST credential.
- Because the MCP client is what starts the MCP server process, that process must detect an app that is not running, a REST service that is off, and an expired or revoked credential, and report each as a distinct, non-retrying error, per F-52. It must never start the app.
- Synthesis runs in a child process governed by an operating-system resource-control facility. On Windows that is what makes the enforced CPU and memory limits in N-03 achievable, and it is also what lets N-21 measure the generation job separately from total app usage. Releasing resources within the five seconds N-22 allows implies aborting mid-segment, which in practice means the worker can be terminated at any instant without corrupting the database or a retained result.
- The GUI must stay usable when the REST port cannot be bound, per F-79, so it must not depend on reaching the service over the network path.

**Build order.** Non-normative, but the sequence matters, because the first two areas cannot be retrofitted onto work that assumes they are absent.

1. The worker process and its operating-system resource container, with the engine behind a single-slot job state machine. N-03 and N-21 decide the process boundary, and N-22's five-second release decides that the worker must be killable mid-segment without corrupting the database or a retained result. Everything else calls into this.
2. The segment pipeline: F-81's segmentation, the normalisation alignment F-27 demands, and F-82's format and concatenation. This is the hardest correctness requirement in the document and every later surface consumes its output. A segment whose source range is wrong is a defect no interface can hide.
3. The reading surface and the streaming player: F-12, F-14, F-26 through F-31, N-12, and N-13. A.5 shows generation outrunning playback roughly fivefold, so the difficulty here is the highlight and the audio clock, not throughput.
4. The library and its database: F-38 through F-45, and N-14 through N-16.
5. The REST service. F-46 makes it live from first launch, so N-17, N-31, and F-71's credential handling belong in its first commit rather than in a hardening pass afterwards.
6. The MCP server last, because F-58 makes it a client of everything above and it can add nothing the REST contract does not already have.

Steps 1 through 3 exercise every genuinely novel requirement here. What follows them is conventional work that gets easier once they exist.

Process layout:

- One application process holding the GUI, the job engine, the database, the local REST service, and audio playback.
- One synthesis worker process at a time, inside an operating-system resource-control container, per F-47, N-03, and N-21.
- One transient MCP server process per client, started by that client and reaching the application over loopback, per F-58.

The desktop client is PySide6, Qt 6 Widgets, in the application process. Each layer below was chosen against a requirement rather than by preference.

| Layer | Choice | Requirement it answers |
| --- | --- | --- |
| Toolkit | PySide6 under LGPLv3. PyQt6 is avoided because it is GPL. | N-11, and N-30, where Qt Widgets has stronger desktop accessibility than the alternatives |
| Reading surface | `QTextEdit` with one view-level extra selection for the current segment | F-29, because an extra selection never enters the document, so copying and storage stay plain; F-30, because it never moves the text cursor; N-13, measured in `spikes/outline_emphasis.py`; and a constant-cost highlight update at 50,000 characters |
| Audio | PortAudio through a callback ring buffer, not the toolkit's media layer | N-12, which needs a sample-accurate frame counter, and F-67, which needs device enumeration and loss detection |
| Local service | FastAPI and uvicorn in a worker thread of the same process | F-79, since the GUI must survive a failed bind and therefore cannot reach the engine over the network path |
| Database | SQLite in write-ahead mode with a bounded busy timeout | N-10, no server to install, and N-14, which forbids waiting indefinitely on a lock |
| Packaging | A PyInstaller directory bundle, then signing and notarization | N-10 describes a folder of an executable and companion files, and that layout is also what keeps Qt as separate shared libraries for LGPL relinking. A single-file bundle satisfies neither. |
| Typeface | One bundled open-licensed Korean and Latin family rather than the per-OS defaults | N-12 and N-30, so metrics and therefore measurements are identical on both operating systems |

Three details follow from the requirements and are easy to get wrong:

- Emphasis is drawn as a glyph outline, and it has to be. An extra selection is a paint-time format, so a weight change applied through one is discarded silently: `spikes/outline_emphasis.py` measures zero changed pixels for bold at every scale factor, on both candidate widgets. An outline is a painting attribute, so it renders, and since nothing in the mechanism can re-shape glyphs, the layout stability N-13 needs comes from the mechanism rather than from the choice of format. The same spike confirms the naive alternative fails: bold merged into the document does reflow, shifting every line below the emphasised one. A pen width of 0.3 to 0.4 reads as emphasis with under one percent of the ink falling outside the emphasised characters' own cells.
- The highlight is driven from the audio callback's frame counter mapped through the segment time table, never from a timer polling a player's reported position. Only the former can hold the 300 ms in N-12, and it is also what yields the separate device-latency figure N-12 asks for.
- The inference runtime is pinned to a local CPU provider by explicit allow-list and its thread count is capped, per F-87. This is not belt-and-braces: the stock runtime also offers a remotely executing provider and picks by default order, so N-01 depends on the allow-list existing. The thread cap is half of F-20, the operating-system resource control being the other half, and A.5 records that capping also happens to be faster.

Alternatives were rejected on requirements rather than taste. A web-view shell cannot select an audio output device on macOS, which breaks F-67. A bundled-browser shell has the wrong memory baseline for the 8 GiB floor in Section 8.1 against the 2 GiB generation budget in F-23. A browser-served UI is ruled out by F-46 and F-79 together, since the owner can turn the service off and the GUI must still work.

### A.3 Decisions Taken

Recorded so implementation does not reopen them, and so a later reader can see what the alternative was and what it cost.

| Ref | Decision | Why, and what was given up |
| --- | --- | --- |
| F-04, F-06, F-08, Section 8.3 | Ship one model, Supertonic 3. | It is the only candidate measured to run inside the default budget, and A.5 shows it doing so with a fivefold margin. What is given up is model choice, which the original F-04 offered as a feature; A.1 records the conditions for restoring it. The selection machinery stays, so the lineup is data rather than structure. |
| F-16, F-03, Section 7 | A job that has not completed can be exported as a clearly labelled partial file. | Four requirements combined to make any interruption of a long job lose everything: a 50,000-character ceiling, one generation slot, no partial export, and no resume. Export is the smallest of the four to relax, because F-82 already defines the concatenation and the segment audio already exists. Resuming stays out of scope, so the file is a snapshot and not a continuation. |
| F-27, N-08 | Emoji and decorative symbols are not synthesised; their source ranges attach to a neighbouring segment. | A.5 measured the engine vocalising three emoji as 1.32 seconds of audio. Skipping costs nothing, and F-27's attachment rule already existed for whitespace, so the highlight still crosses them and the source text stays complete. Speaking them was the alternative, and is the outcome a listener is least likely to want. |
| F-54, Section 2.10, Section 4.2 | Every job request and representation carries an explicit job kind, with one accepted value in this release. | Adding a second job kind to an unnamespaced path later is a breaking change for every client. A field with one legal value reveals nothing about future scope and costs nothing now. Namespacing the paths was the alternative and would have rewritten the contract table for no present benefit. |
| F-19 | The model is still released on cancellation, as originally written. | The concern was that a cancel, adjust, regenerate loop would pay a full reload each time. A.5 measured warm load at 0.8 seconds for the shipped model, so the cost is negligible and the simpler rule stands. Revisit only if a model heavy enough to be worth keeping resident ever ships. |
| F-47, F-50 | Requests carry no priority by path; the owner reclaims the slot by cancelling. | Preemption for GUI requests would require deciding what happens to a client's half-finished job, a policy F-49 and F-50 do not have. Cancelling is already reachable in one place per F-69. The cost is that a client running at the rate limit can hold the slot until the owner intervenes, which F-50 now states rather than implies. |

### A.4 Verification Backlog

What is still unverified, and who has to resolve it. None of it blocks starting implementation; the first three block release.

- Voice quality, meaning N-08 and the Section 8.2 review sample. A.5 measured throughput, footprint, and conformance only. Nobody has listened to the output in either language, and Section 8.3 makes missing speech, unnecessary repetition, corrupted audio, and clearly incorrect sentence output release-blocking defects. This needs ears, and it needs them early, because the lineup is now a single model with no fallback.
- Legal review of the shipped model's licence. It is a responsible-AI licence whose paragraph 5 obliges a distributor to bind its own users to the same use restrictions, which N-11 and F-80 now treat as a release blocker. Its clause 7 also reserves the licensor's right to restrict use of the model remotely and asks distributors to make reasonable efforts to stay current. Nothing obliges the product to implement remote control, and N-01 and F-75 forbid acting without the user, but that clause should be read by someone qualified against a product that is deliberately offline and updates only on consent.
- Windows code signing, and Apple Developer signing and notarization, for N-29. Procurement lead time, needed well before release.
- Whether `QPlainTextEdit` can replace `QTextEdit` as the reading surface. It is the cheaper widget for a 50,000-character document and its pixel behaviour is identical, but `spikes/outline_emphasis.py` cannot prove its layout stability: its `cursorRect` sweep is not idempotent, and its document-level control does not reflow far enough to measure an outline against. Worth resolving if the heavier widget proves slow, and not before.

### A.5 Measured Results

From `spikes/tts_feasibility.py`, run on 20 logical CPUs and 31.8 GiB RAM. That machine is larger than Section 8.2's baseline, so thread counts were capped to emulate it: the baseline is 8 logical CPUs under F-20's 20 percent default, about 1.6 cores, and the figures below are the 2-thread rows. Supertonic numbers are measured here; Qwen numbers are third-party and are labelled as such.

| | Supertonic 3 | Qwen3-TTS 0.6B CustomVoice | Qwen3-TTS 1.7B CustomVoice |
| --- | --- | --- | --- |
| Weights | obtainable, official | obtainable, official | obtainable, official |
| Model license | BigScience OpenRAIL-M | Apache-2.0 | Apache-2.0 |
| On disk | 0.38 GB | 2.33 GB | 4.21 GB |
| Native sample rate | 44,100 Hz | 24,000 Hz | 24,000 Hz |
| Voices | M1 to M5, F1 to F5 | 9 built-in, 4 female | 9 built-in, 4 female |
| Korean | yes, 1 of 31 languages | yes, 1 native voice of 9 | yes, 1 native voice of 9 |
| Working memory | 0.57 GB measured | about 3 GB reported | about 8 GB reported |
| Real-time factor | 0.18 to 0.21 measured | 0.52 int4, all cores, reported | 0.57 to 0.95 int4, all cores, reported |
| Windows CPU path | yes, ONNX Runtime | none first-party | none first-party |
| Verdict | streams with margin | over budget on the minimum machine | cannot meet Section 8.1 |

Supertonic detail: 44,100 Hz mono 16-bit, matching F-82. Ten voice styles, matching F-06 exactly. Warm load 0.8 seconds, so F-17's reload cost is negligible. Real-time factor at the baseline ranges from 0.067 at the fastest quality setting to 0.207 at the default, meaning roughly five to fifteen times faster than real time, and even a single thread stays under 0.36. F-12's continuous playback is safe with this model.

F-12 was then replayed the way the product will actually run it, segment by segment rather than as one call, since per-call overhead and the first segment's length are what decide whether playback stalls. Over a five-sentence Korean document at the baseline, first audio was ready 1.02 seconds after the request and the buffer margin grew monotonically from 5.1 to 23.2 seconds ahead of the playhead. Generation outruns playback by roughly five to one, so F-12 holds with margin and N-07's unavoidable initial wait is about a second on a warm model. Under F-81's eight-second cap on the first segment the worst case is near 1.6 seconds.

Two findings worth carrying into implementation:

- More threads is slower. Twenty threads ran about twice as slow as two, a classic oversubscription effect. F-20's CPU cap is therefore not only the budget-compliant configuration but also the fast one, which removes the usual tension between the two.
- The stock inference runtime exposes a remotely executing provider alongside the local one, and selects providers by default order. N-01's guarantee that nothing is sent to an external service is not automatic; it holds only because F-87 requires an explicit local-only allow-list.

`spikes/engine_conformance.py` then checked the ranges and edge cases the requirements name against the engine itself, rather than against intent. F-07's 0.70x to 1.50x span is real, giving a 2.1x spread in duration. F-05's automatic mode exists. F-03's empty and whitespace-only inputs raise rather than synthesise, so the app's own validation is the guard the requirement says it is, and F-25 has something to catch. Every F-27 edge case survived without an exception: mixed Hangul and Latin, numbers and dates, symbols, line breaks, repeated sentences, and runs of wide whitespace.

Three results changed how something should be built:

- The engine's inter-sentence pause parameter had no effect at all: 0.0, 0.3 and 0.8 seconds produced audio of identical length, because it applies only when the engine chunks text internally and the product chunks it first. Inter-segment silence is therefore the app's own to insert, which is what F-82 already describes and what makes the segment time ranges in F-27 exactly known instead of inferred. Do not route F-08's speaking-style pauses through that parameter.
- Emoji are not silent. Three emoji alone produced 1.32 seconds of audio, so the engine vocalises them rather than skipping them. The amended F-27 rule about spans that produce no audio is still needed for whitespace, but emoji are the opposite problem and are a product decision, recorded in A.3.
- F-41's warning that identical settings do not guarantee identical audio is not a formality. Two consecutive runs of the same text with the same settings gave the same length but a maximum sample difference of 0.47 on a signal bounded at 1.0, which is audible variation, not floating-point noise. Nothing may assume a regenerated result reproduces a stored one.

Quality has not been assessed. These are throughput, footprint, and conformance measurements only; N-08 and the Section 8.2 review sample remain outstanding.
