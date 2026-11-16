# Camera viewing prerequisite: reject controls and scene membership

Enabling safe camera discovery makes camera devices newly authorizable. Existing
legacy command and scene paths therefore need independent addon defense before
the viewing writer can roll out. Backend guards are prepared separately; this
commit adds no endpoint, deployment, version or database schema change.

The actual addon command handler rejects direct camera targets and camera-qualified
service actions before dispatch. The low-level service boundary also rejects the
camera domain and mixed homeassistant targets containing a camera. It rejects
camera entities/snapshots in direct scene service payloads. The live JPEG worker
continues to make only authenticated snapshot GET requests, never camera controls.

Scene create/update rejects the complete operation if either explicit entities or
snapshot_entities contains a camera, even if the other input would otherwise win.
The check occurs before HA state lookup, attribute expansion, service dispatch or
SQLite save. A resolved source explicitly typed as a camera is rejected before
copying attributes too. There is no silent partial scene and no camera token,
entity_picture, stream URL or nested credential copied into a new definition.

Legacy stored camera scene rows are not altered or deleted. They are skipped by
restoration and rejected on activation. Existing scene tombstones stay intact.
For imported scenes, activation now requires current HA metadata with a nonempty
entity_id member list; unknown membership fails closed, and any camera member
rejects the entire activation. A known camera-free imported light scene still
runs. Generic scene turn_on commands use the same checked activation path instead
of bypassing it. This can reject imported scenes whose integration does not expose
membership, an intentional prerequisite rather than guessing that they are safe.

Eleven new tests exercise the actual extracted addon handlers with synthetic
inputs only. The first nine tests failed before the guards were added. Coverage
includes mixed snapshot rejection before lookup, atomic explicit update rejection,
camera secrets never reaching saved definitions, ignored snapshot arguments,
legacy scene restoration/activation, imported unknown/camera/light membership,
legacy scene.create/update command shapes, camera-typed aliases, generic scene
activation and direct/low-level camera controls. No test contacts HA or operates
any device. All 99 addon tests pass, including existing non-camera scene CRUD,
tombstone, receipt durability and both-database preservation checks. Compilation
and whitespace checks pass.

No customer scene or existing HA definition was inspected, changed or purged.
Root-controlled compatible backend readers and control/scene denial must precede
any addon upgrade. Exact-source image CI, official generated-HA/new-worker proof
and production rollout/database-preservation gates remain separate.
