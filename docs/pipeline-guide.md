# exaSPIM SWC processing pipeline — guide to the code

This walks through the pipeline stage by stage: what each capsule does, every file in it,
every function, and how the pieces hand data to one another.

The pipeline turns refined exaSPIM neuron reconstructions into CCF-registered SWCs and
publishes **one AIND derived data asset per neuron**.

---

## 1. The big picture

Five repositories, all under `peter-grotz` on GitHub:

| Repository | Role | Code Ocean |
|---|---|---|
| [`exaspim-swc-transform-capsule`](https://github.com/peter-grotz/exaspim-swc-transform-capsule) | Stage 1: specimen space → CCF space | capsule 2015425 |
| [`exaspim-swc-resample-capsule`](https://github.com/peter-grotz/exaspim-swc-resample-capsule) | Stage 2: resample both spaces at 10 µm | capsule 9001304 |
| [`exaspim-swc-packaging-capsule`](https://github.com/peter-grotz/exaspim-swc-packaging-capsule) | Stage 3: one AIND asset per neuron | capsule 9464795 |
| [`exaspim-swc-processing`](https://github.com/peter-grotz/exaspim-swc-processing) | Shared library all three install | — |
| [`exaspim-swc-processing-pipeline`](https://github.com/peter-grotz/exaspim-swc-processing-pipeline) | The Nextflow pipeline wiring them | pipeline 8900412 |

The capsules are deliberately thin. Each one reads its inputs, calls the library, and
writes its outputs. Anything with real logic — metadata, naming, geometry, layout — lives
in the library, where it is tested to 100% coverage.

### Data flow

```
 reconstruction asset (Collect)          processed imaging asset (S3 + DocDB)
 dispatch/  refinement/final-world/       ccf_alignment/  fusion/fused_ccf_ch.zarr
            refinement/final-voxel/       acquisition.json
        │                                        │
        └────────────────┬───────────────────────┘
                         ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │ 1. TRANSFORM   final-world SWCs ──ANTs──▶ CCF-space SWCs          │
  │    writes: alignment/aligned_swcs/  alignment/acquisition.json    │
  │            alignment/data_process.json                            │
  │    carries forward: dispatch/  refinement/                        │
  └──────────────────────────────────────────────────────────────────┘
                         │  Nextflow passes /results → next /data
                         ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │ 2. RESAMPLE    SNT resampling at 10 µm, both coordinate spaces    │
  │    writes: final/ccf_space_reconstructions/swcs/                  │
  │            refinement/final-voxel-resampled/                      │
  │            final/data_process.json                                │
  │    carries forward: dispatch/  refinement/  alignment/            │
  └──────────────────────────────────────────────────────────────────┘
                         │
                         ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │ 3. PACKAGING   regroup by neuron, write AIND metadata             │
  │    writes: <asset-name>/ per neuron, at the results root          │
  └──────────────────────────────────────────────────────────────────┘
```

### Why "carry forward" exists

Nextflow gives each process **only the previous process's `/results`** as its `/data`. If
transform didn't republish `refinement/`, resample would never see the specimen-space
reconstructions, and packaging would never see anything but resample's own output. So each
stage copies (hardlinks, where possible) the upstream stage directories into its own
results. See `stage.carry_forward`.

### What each neuron's asset looks like

```
exaSPIM_841260_2026-07-07_15-13-51_reconstruction-N024_2026-09-23_22-55-02/
├── data_description.json
├── processing.json
├── specimen_space_reconstructions/
│   ├── refined/N024-841260-DR.swc        voxel units, as refined
│   └── resampled/N024-841260-DR.swc      voxel units, 10 µm node spacing
└── ccf_space_reconstructions/
    └── N024-841260-DR.swc                CCF µm, 10 µm node spacing
```

Only `data_description.json` and `processing.json` are written as metadata. That is the
complete required set for a processing-type derived asset in `aind-data-schema`; the
parent's `subject`, `procedures`, `acquisition` and `instrument` are deliberately not
copied in.

---

## 2. How a capsule actually runs on Code Ocean

Three separate things determine what code a run executes, and they change independently.
Most of the debugging on this pipeline came from confusing them.

| What | Where it comes from | When it updates |
|---|---|---|
| **Capsule code** (`code/`) | The capsule's Code Ocean git `main`, cloned at launch | After **Sync with GitHub** |
| **The library** | `pip install ...@<commit SHA>` in the environment file | After a **SHA bump + environment rebuild** |
| **Dataset mounts** | Connections drawn on the pipeline canvas | After **committing the pipeline** |

So:

- Changing a capsule's `code/` → merge on GitHub, then **Sync with GitHub**. No rebuild.
- Changing the library → merge, bump the pinned SHA in each capsule's environment file,
  merge that, Sync, then **rebuild each environment**.
- Changing which data a stage sees → edit and commit the pipeline canvas.

Datasets must be connected as **Collect**, not Default. Default sends each top-level item
of an asset to a separate parallel instance of the capsule.

### App Builder parameters arrive as ordered values

Code Ocean passes a capsule's App Builder parameters as bare positional values —
`./run <value1> <value2> …` — not as `--flags`. Every capsule's `code/run` translates those
slots into the flags `run_capsule.py` parses, drops blank ones, and passes real flags
through untouched when a capsule is invoked from a shell.

---

## 3. Stage 1 — transform capsule

**Job:** move each reconstruction from specimen space into CCF space using the sample's
own CCF registration.

**Inputs:** the reconstruction asset (mounted), the processed imaging dataset (read from
S3 and DocDB), and three mounted reference assets: the CCF template, the exaSPIM template,
and the template→CCF transform `reg_exaspim_template_to_ccf_25um_v1.5`.

**Parameters:**

| Slot | Meaning |
|---|---|
| `swc_dir` | **Physical-space** reconstructions: `<asset>/refinement/final-world`. `final-voxel` is rejected. |
| `processed_dataset` | The processed imaging asset, e.g. `exaSPIM_841260_2026-07-07_15-13-51_processed_2026-07-26_09-25-11` |
| `df_asset` | Optional manual CCF-refinement displacement field: a file, or a directory holding exactly one `.nrrd`/`.nii.gz` |
| `experimenters` | Optional, comma-separated, recorded on the stage |

### `environment/Dockerfile`

Starts from Code Ocean's `mambaforge3` Python 3.12 image, drops to Python 3.11 (allensdk
2.16.2, required by `aind_exaspim_register_cells`, caps numpy below 1.24, which cannot build
on 3.12), then pip-installs `aind_exaspim_register_cells` and the library at a pinned
commit.

### `code/run`

The entry point Code Ocean calls. Records `pip list` to `/results/pip_list.txt`, puts
`code/src` on `PYTHONPATH`, maps the four positional slots onto `--swc-dir`,
`--processed-dataset`, `--df-asset`, `--experimenters`, and runs `run_capsule.py`.

### `code/run_capsule.py` — the stage's main program

Runs top to bottom in `run()`; every other function is one step of it.

| Function | What it does |
|---|---|
| `parse_args()` | Reads `--swc-dir`, `--processed-dataset`, `--df-asset`, `--experimenters`, `--fail-fast`, each falling back to an environment variable. |
| `scratch_dir()` | Returns `/scratch/exaspim_swc_transform` on Code Ocean (`/tmp/...` elsewhere), creating it. |
| `reconstruction_root(swc_dir)` | Walks up from `swc_dir` to the directory holding `refinement/`/`dispatch/`. A mounted asset puts those one level below `/data`, so this is what makes carry-forward find them. |
| `locate_bundle(spec)` | If `processed_dataset` is a local directory, uses it; otherwise stages the registration files from S3 (`s3_stage.stage_registration_bundle`). |
| `locate_dataset(spec, bundle)` | Works out the bucket and processed dataset name — from the URI or path if they carry it, otherwise by S3 lookup for a bare subject id. |
| `load_reference_images(resolved, bucket, dataset)` | Disables the unused overlay normalisation, derives the reference geometry, and builds the images the registration needs — without loading the two multi-GB reference volumes. |
| `transform_one(swc_path, pipeline, images, destination)` | Reads one SWC (applying any `# OFFSET` header), converts its nodes through `preprocess_coords` and `apply_transforms_to_points`, scales the resulting CCF indices by 10 µm, and writes it. |
| `transform_all(swc_dir, pipeline, images, destination, fail_fast)` | Runs `transform_one` over every SWC. A failing cell is logged and skipped unless `--fail-fast`. Returns how many were found and which failed. |
| `carry_acquisition(source, output_root)` | Copies `acquisition.json` into `alignment/`. Resample needs its `coordinate_transformations` for voxel↔physical conversion, and this is the only stage that has it. |
| `run()` | The sequence: check `swc_dir` (and reject `final-voxel`) → locate bundle and dataset → carry forward `dispatch/`, `refinement/` → resolve inputs → resolve the acquisition from DocDB → build `RegistrationPipeline` → load reference images → transform all → carry the acquisition → write `alignment/data_process.json`. Exits 1 if any cell failed. |

**Why `final-world` and not `final-voxel`:** the registration library converts physical
coordinates to voxels itself, dividing x and y by the 0.748 anisotropy. Voxel input gets
converted twice and lands 1.337× too large in-plane and displaced. The guard in `run()`
refuses it.

### `code/src/exaspim_swc_transform/io_swc.py`

| Function | What it does |
|---|---|
| `read_swc_offset(path)` | Returns the x/y/z from a `# OFFSET` header line, or `None`. |
| `read_swc(path, add_offset)` | Reads an SWC with allensdk and, if it has an offset header, adds it to every node. |

### `code/src/exaspim_swc_transform/s3_stage.py`

Fetches the per-sample registration files from the public `aind-open-data` bucket
anonymously.

| Function | What it does |
|---|---|
| `_client()` / `s3_client()` | An unsigned boto3 S3 client. |
| `resolve_dataset(spec, bucket)` | Turns an `s3://` URI, a dataset name, or a bare subject id into `(bucket, dataset)`. A subject id picks the newest processed dataset that has `ccf_alignment/`. |
| `stage_registration_bundle(spec, dest_root, bucket)` | Downloads the sample→template affine, the inverse warp and `acquisition.json` into a local bundle. It no longer downloads the reference volumes. |

### `code/src/exaspim_swc_transform/transform_resolution.py`

Finds every file `RegistrationPipeline` is built from, whatever shape the bundle arrived in.

| Name | What it does |
|---|---|
| `MissingInput` | Raised when a required input is absent. |
| `ResolvedInputs` | Dataclass of absolute paths: acquisition, reference volumes (may be empty), both transform pairs, the manual displacement field, both templates. |
| `_resolve(explicit, candidates, what, required)` | The one lookup helper: use an explicit path if given, else the first candidate that exists; blank or error if none. |
| `_bundle_root(transform_dir)` | `transform_dir/ccf_alignment` if present, else `transform_dir`. |
| `_infer_dataset_id(bundle_root, transform_dir)` | Finds the six-digit subject id in surrounding directory or file names. |
| `_resolve_manual_df(manual_df_path, dataset_id)` | A file is used directly; a directory is searched for a single `.nrrd`/`.nii.gz` without assuming a filename; none or several is an error. |
| `resolve_inputs(transform_dir, manual_df_path, dataset_id, …)` | Builds the full `ResolvedInputs`. The template→CCF transform defaults to v1.5. |

### `code/src/exaspim_swc_transform/reference.py`

The registration library would read two NIfTI reference volumes of 1.4–1.8 GB each, but
only ever uses their **shape and geometry**, and many datasets never published them. This
module replaces them with geometry-only stand-ins.

| Name | What it does |
|---|---|
| `ReferenceImages` | Dataclass passed to `transform_one`: the CCF template, the exaSPIM template, and the two stand-ins. |
| `disable_overlay_normalization()` | Makes `ImageVisualizer.perc_normalization` a pass-through. It only feeds QC overlays this pipeline doesn't draw, it's the most expensive call in the stage, and it asserts on constant input. |
| `_published_geometry(client, bucket, key)` | Reads a published reference volume's shape and spacing from a 200 KB byte range of its header, or `None` if it wasn't published. |
| `_derive_geometry(client, bucket, dataset)` | Derives both geometries from the registration's own `ccf_alignment/processing.json` and the zarr level it read. |
| `resolve_geometry(client, bucket, dataset, dataset_id)` | Uses whichever source exists. Where **both** exist, checks them against each other and fails loudly on disagreement. |
| `build_reference_images(ccf_path, exaspim_template_path, loaded, resampled)` | Reads the two real templates and pairs them with the stand-ins. |

---

## 4. Stage 2 — resample capsule

**Job:** resample every reconstruction to a fixed 10 µm node spacing, in both coordinate
spaces.

**Parameters:** `spacing_um` (default 10), `experimenters`.

### `environment/Dockerfile` and `environment/postInstall`

The Dockerfile only runs `postInstall`, which:

1. Drops to Python 3.11.
2. Installs a JVM with `pyimagej` and `scyjava`, because resampling uses SNT (Fiji).
3. Removes the conda package cache with `rm -rf` — **not** `mamba clean`. The JVM install
   upgrades `libxml2`, which breaks mamba's own bindings, so any later mamba call fails.
4. pip-installs `neuron-tracing-utils` (pinned commit), the library (pinned commit), and
   **`jgo==2.2.0`**. `scyjava 1.12.0` requires `jgo` with no upper bound, and `jgo` 3.0
   removed a name scyjava imports at startup.

### `code/run`

Sets up the JVM environment: `JAVA_HOME`, `FIJI_PATH=/data/Fiji-builds/Fiji.app`, and
`JAVA_OPTS` with `-Xms1g -Xmx16g`. A small `-Xms` matters: a large one claims that memory
at JVM startup and puts a floor under the container size. Then maps the two positional
slots and runs `run_capsule.py`.

### `code/run_capsule.py`

| Function | What it does |
|---|---|
| `parse_args()` | Reads `--spacing-um` and `--experimenters`. |
| `resample(source, destination, spacing_um)` | Runs `python -m neuron_tracing_utils.resample` on a directory. It loads one SNT `Tree` at a time. |
| `find_dir(*candidates)` | Returns the first of several relative paths that exists under `/data`. |
| `matched_pair(voxel_dir, world_dir)` | Finds one reconstruction present in both spaces, for deriving the scale. |
| `resample_specimen_space(spacing_um, output_dir)` | Resamples the **physical** reconstructions (so spacing is truly 10 µm on every axis), then converts them back to voxels with the acquisition's scale. The voxel grid is anisotropic, so resampling in voxels directly would space nodes unevenly. |
| `run()` | Carry forward → resample `alignment/aligned_swcs` into `final/ccf_space_reconstructions/swcs` → resample specimen space into `refinement/final-voxel-resampled` → write `final/data_process.json`. |

The module docstring still says "annotate the CCF-space ones"; annotation was removed and
the docstring is stale.

### `code/scale.py`

Voxel ↔ physical conversion for specimen-space SWCs.

| Name | What it does |
|---|---|
| `MalformedSwcError`, `ScaleDerivationError` | Raised for an unreadable SWC line, or a scale that can't be derived consistently. |
| `scale_from_acquisition(acquisition_json)` | Reads the voxel size from the acquisition's `coordinate_transformations`. The preferred source. |
| `_iter_transforms(payload)` | Walks a nested acquisition record yielding every transform entry. |
| `_node_fields(line, path)` | Splits one SWC node line, rejecting a truncated one. |
| `_read_points(path, limit)` | Reads the first coordinates of an SWC. |
| `derive_scale(voxel_swc, world_swc, sample_nodes)` | Fallback: recovers the scale from a matched voxel/world pair, checking the same ratio holds on every sampled node. |
| `scale_swc(source, destination, factors)` | Writes an SWC with every coordinate multiplied by per-axis factors. |

---

## 5. Stage 3 — packaging capsule

**Job:** regroup everything by neuron and write each one as a valid AIND derived asset.

**Parameters:** `parent_asset` (normally blank — inferred), `modalities` (used only if the
parent has none).

### `environment/Dockerfile`

Python 3.12 base, pip-installs only the library at a pinned commit.

### `code/run`

Maps the positional slots and runs `run_capsule.py`.

### `code/run_capsule.py`

| Function | What it does |
|---|---|
| `parse_args()` | Reads `--parent-asset`, `--modalities`, and `--pipeline-url/-name/-version` (from the `PIPELINE_*` environment variables `nextflow.config` sets). |
| `load_stage_processes(data_dir)` | Reads each stage's `data_process.json` from `dispatch/`, `refinement/`, `alignment/`, `final/`. |
| `infer_parent_asset(processes)` | Recovers the parent imaging asset from the `processed_dataset` the transform stage recorded. |
| `run()` | Resolve the parent's `data_description` (DocDB v2 → v1 → S3, upgrading if needed) → build the pipeline `Code` from `PIPELINE_*` → `package_cells` → log any skipped cells. |

Packaging does **not** record itself as a `DataProcess`: it hardlinks files and writes
metadata without changing any data.

---

## 6. The shared library — `exaspim-swc-processing`

`src/exaspim_swc_processing/`, one module per concern.

### `stage.py` — used by transform and resample

| Function | What it does |
|---|---|
| `_resources()` | Describes the machine, reading the allocated CPUs from `CO_CPUS`. |
| `installed_version(distribution)` | The installed version of a package — the fallback for `Code.version`, since the running capsule never sees its own commit. |
| `resolve_code(name, url, version, …)` | Builds the `Code` record for a stage from the environment. |
| `_with_parameters(code, parameters)` | Attaches runtime parameters to a `Code`, validating them. |
| `build_stage_process(...)` | Builds a stage's `DataProcess`: timing, code, parameters, outputs, experimenters, resources. |
| `write_stage_process(process, output_dir)` | Writes it as `data_process.json`. |
| `carry_forward(data_dir, results_dir, stages)` | Republishes upstream stage directories into this stage's results, hardlinking where possible. |

### `acquisition.py` — used by transform

| Name | What it does |
|---|---|
| `acquisition_sources(host, bucket)` | The lookup order: DocDB v2, DocDB v1, then `acquisition.json` in S3. |
| `resolve_acquisition(asset_name, destination, sources, local_fallback)` | Fetches the first one that answers, writes it to a file, and returns the file and its source. A staged copy is the last resort. |
| `AcquisitionNotFoundError` | Raised when nothing answers. |

### `registration.py` — used by transform's `reference.py`

Recovers the registration's reference geometry from metadata.

| Name | What it does |
|---|---|
| `VolumeGeometry` | A volume's shape and spacing. |
| `RegistrationPass` | One pass from the registration record: input zarr, level, resolution, scale, transform paths. `loaded_spacing_mm` is a constant of the pass; the recorded `sample_scale` is ambiguous between records and isn't used for geometry. |
| `parse_registration_record(record, resolution_um)` | Extracts the 10 µm pass from `ccf_alignment/processing.json`. |
| `loaded_geometry(pass_, zarr_shape)` | The loaded volume: the zarr level's shape reordered to `(x, z, y)`. |
| `resampled_geometry(loaded)` | The 10 µm isotropic volume, computed with exact fractions. Float division rounds 1270 × 1.35 to the wrong side. |
| `zarr_level_key(pass_)`, `zarr_shape(zarray)` | Where the zarr level's `.zarray` lives, and its spatial shape with leading singleton axes dropped. |
| `parse_nifti_geometry(compressed_header)` | Shape and spacing from the first bytes of a gzipped NIfTI. |
| `registration_volume_key(dataset, dataset_id, volume)` | Where a published reference volume would be. |
| `reconcile(derived, published, volume)` | Fails if derived and published geometry disagree. |
| `dataset_name(*candidates)` | Pulls a processed dataset name out of a URI, a path or a bare name. |

### `reference.py` — used by transform's `reference.py`

| Name | What it does |
|---|---|
| `reference_array(geometry)` / `reference_arrays(loaded, resampled)` | Zero-strided read-only arrays with the right shape. A 1.69 GB volume becomes ~50 KB. |
| `apply_geometry(image, geometry)` | Stamps spacing, origin and direction onto an image. |

### `layout.py` — used by packaging

| Name | What it does |
|---|---|
| `ArtifactSpec`, `ARTIFACT_SPECS` | Where each artifact comes from and where it goes: refined voxel SWCs → `specimen_space_reconstructions/refined`, resampled → `.../resampled`, CCF → `ccf_space_reconstructions`. |
| `CellArtifacts` | The files found for one neuron; `missing_roles()` lists required ones that are absent. |
| `resolve_source_dir(stage_root, spec)` | Finds which candidate source directory exists. |
| `discover_cells(stage_root, specs)` | Indexes every stage's outputs by reconstruction stem. |
| `place_artifact(source, destination)` | Hardlinks, falling back to a copy. |
| `write_cell_directory(cell, cell_dir, specs)` | Writes one neuron's files into its directory. |

### `naming.py` — used by packaging

| Name | What it does |
|---|---|
| `parse_stem(stem)` → `ReconstructionId` | Splits `N024-841260-DR` into neuron, subject and annotator. The annotator is kept so `N001-794492-HP` and `N001-794492-LP` don't collide. |
| `derived_asset_name(primary_asset_name, reconstruction, creation_time)` | Builds the AIND asset name. |

### `data_description.py` — used by packaging

| Name | What it does |
|---|---|
| `as_derived(parent)` | A copy of the parent with `data_level` corrected to derived (exaSPIM processed assets declare `raw`). |
| `derive_cell_data_description(parent, reconstruction, creation_time)` | One neuron's `data_description`, inheriting funding, investigators, project, modalities and license from the parent. |

### `processing.py` — used by packaging

| Name | What it does |
|---|---|
| `linear_dependency_graph(processes)` | Chains stages in chronological order. |
| `build_cell_processing(processes, pipeline, …)` | One neuron's `processing.json`: all stage records, the pipeline `Code`, the dependency graph. |

### `parent_metadata.py` and `sources.py` — used by packaging and transform

| Name | What it does |
|---|---|
| `docdb_fetcher(version, host, client, field)` | Reads one field of an asset's DocDB record (`data_description`, `acquisition`, …). |
| `s3_fetcher(bucket, client, filename)` | Reads one JSON file from an asset's S3 root. |
| `default_sources(host, bucket)` | DocDB v2 → v1 → S3, for `data_description`. |
| `upgrade_data_description(document)` | Upgrades an old-schema document with `aind-metadata-upgrader`. |
| `resolve_parent_metadata(asset_name, sources, upgrader)` | Tries each source, upgrades if needed, and records where it came from. |
| `ParentMetadata.tags()` | Tags like `metadata-source:docdb_v1`, `metadata-upgraded`, written onto each neuron's `data_description`. |

### `packaging.py` — used by packaging

| Name | What it does |
|---|---|
| `package_cells(stage_root, output_root, parent, stage_processes, pipeline, creation_time, …)` | For every neuron: derive its `data_description` (skipping it with a warning if that fails), write its files, write both metadata files. |
| `PackagedCell`, `SkippedCell`, `PackagingResult` | What was written and what was skipped. |

---

## 7. The pipeline repository

| File | What it is |
|---|---|
| `pipeline/main.nf` | **Generated by Code Ocean's pipeline editor — never edit it.** One process per capsule, their CPU/memory, dataset mounts, and the wiring. The copy in git can lag what actually runs: Code Ocean resolves each capsule's commit at launch. |
| `pipeline/nextflow.config` | Hand-written. Sets `PIPELINE_URL`, `PIPELINE_NAME`, `PIPELINE_VERSION` (packaging records them in `processing.json`) and the cost-monitoring resource label. |
| `CHANGELOG.md` | Pipeline version history; must agree with `PIPELINE_VERSION`. |
| `.github/workflows/release.yml.disabled` | AIND's release workflow, parked until the repo moves to AllenNeuralDynamics. |

Current resources: transform and resample 8 CPU / 60 GB, packaging 1 CPU / 7.5 GB.

---

## 8. Known issues

- **Packaging's `experimenters` panel parameter no longer works.** It was removed from
  `run_capsule.py` but remains in the App Builder panel and in `code/run`'s slot mapping.
  Blank is harmless; a value fails the stage with an argparse error.
- **Unused code:** `layout.build_cell_layout`, `ParentMetadata.provenance()`, and
  `RegistrationPass.template_to_ccf_version` are no longer called.
- **Stale docstring:** resample's `run_capsule.py` still mentions annotation.
- **`tools/preflight.py`** reads the `main.nf` in git, which isn't what runs, so its
  pin warnings are unreliable.
- **Template version:** every registration record names template→CCF v1.4; the pipeline
  applies v1.5 by decision. Worth remembering if CCF placement shows a residual offset.
