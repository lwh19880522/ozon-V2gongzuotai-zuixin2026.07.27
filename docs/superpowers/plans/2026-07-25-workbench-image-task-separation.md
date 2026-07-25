# Workbench Image Task Separation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the workbench upload each product with one locked 1688 original image and emit a durable image-generation-and-upload package without receiving final images.

**Architecture:** Remove generated-image readiness and R2 publication from the workbench upload path. Use the locked subject-master URL as the only bootstrap image, then atomically emit a secret-free package under the runtime-wide image-task inbox after Seller API submission.

**Tech Stack:** Python 3.11, unittest/pytest, Pillow only in the independent image workflow, filesystem JSON contracts, Ozon Seller API adapter.

---

### Task 1: Persist image task packages

**Files:**
- Modify: `src/ozon_v2/adapters/fs_repo.py`
- Test: `tests/test_workbench_local_server.py`

- [ ] **Step 1: Write the failing repository-contract test**

Add a test that calls `save_image_task_package()` twice with the same package ID,
asserts the fixed path is under `runtime_root/image_tasks/pending`, and verifies
the JSON is readable and contains the same package ID.

- [ ] **Step 2: Run the repository-contract test**

Run:
`python -m pytest tests/test_workbench_local_server.py -k image_task_package_repository -q`

Expected: FAIL because the repository methods do not exist.

- [ ] **Step 3: Implement fixed inbox helpers**

Add:

```python
def image_task_pending_dir(self) -> Path:
    path = self.context.runtime_root / "image_tasks" / "pending"
    path.mkdir(parents=True, exist_ok=True)
    return path

def save_image_task_package(self, package_id: str, payload: dict[str, Any]) -> Path:
    path = self.image_task_pending_dir() / f"{safe_filename(package_id)}.json"
    self._write_json(path, payload)
    return path
```

Use the repository's existing filename sanitization and atomic JSON writer.

- [ ] **Step 4: Run the repository-contract test**

Expected: PASS.

### Task 2: Remove generated images from product readiness

**Files:**
- Modify: `src/ozon_v2/services/workbench_service.py`
- Test: `tests/test_workbench_local_server.py`

- [ ] **Step 1: Write failing upload-readiness tests**

Create a supplier-review run with a locked subject master, complete fields and
pricing, but no completed image job. Assert:

```python
item["ready_to_build"] is True
item["generated_images_ready"] is False
"images" not in item["blocking_gates"]
item["bootstrap_image_ready"] is True
```

- [ ] **Step 2: Run the readiness tests**

Run:
`python -m pytest tests/test_workbench_local_server.py -k upload_no_longer_waits_for_generated_images -q`

Expected: FAIL because the current upload gate still requires eight generated
images.

- [ ] **Step 3: Implement bootstrap-image readiness**

Load `subject_master` for each seed, validate its first URL and hash-bound
selection, expose `bootstrap_image_url`, and replace the generated-images
blocking gate with a `bootstrap_image` gate. Keep generated-image fields only as
legacy diagnostics.

- [ ] **Step 4: Run the readiness tests**

Expected: PASS.

### Task 3: Upload with one locked original image

**Files:**
- Modify: `src/ozon_v2/services/workbench_service.py`
- Test: `tests/test_workbench_local_server.py`

- [ ] **Step 1: Replace the existing upload test with a failing one-image test**

Assert that `preview_product_upload()`:

```python
assert preview.ok
assert preview.data["seller_api_item"]["images"] == [locked_subject_url]
assert preview.data["seller_api_item"]["primary_image"] == locked_subject_url
assert publisher.published_products == []
```

Also assert the preview succeeds without public-media configuration and without a
completed image job.

- [ ] **Step 2: Run the one-image preview test**

Expected: FAIL because the current implementation requires eight local generated
images and publishes them to R2.

- [ ] **Step 3: Implement the one-image payload**

Remove `reviewed_image_files`, aspect-ratio validation, and R2 publication from
`preview_product_upload()`. Build `_seller_api_import_item()` with a one-element
list containing the locked subject-master original URL.

- [ ] **Step 4: Run upload preview and submission tests**

Run:
`python -m pytest tests/test_workbench_local_server.py -k "product_upload or upload_no_longer" -q`

Expected: PASS.

### Task 4: Emit the image package after product submission

**Files:**
- Modify: `src/ozon_v2/services/workbench_service.py`
- Modify: `tests/helpers.py`
- Test: `tests/test_workbench_local_server.py`

- [ ] **Step 1: Write the failing package-emission test**

Submit one confirmed product and assert:

```python
package_path = Path(submitted.data["image_task_package_path"])
package = json.loads(package_path.read_text(encoding="utf-8"))
assert package["status"] == "pending"
assert package["store_target"]["seller_import_task_id"] == 7001
assert package["bootstrap_image"]["url"] == locked_subject_url
assert package["generation_contract"]["return_to_workbench"] is False
```

Assert a repeated idempotent submit does not create a second package.

- [ ] **Step 2: Run the package-emission test**

Expected: FAIL because submission currently records only the Seller task.

- [ ] **Step 3: Build and persist the package**

Add a private `_emit_image_task_package()` helper that derives a stable package
ID from `run_id`, `seed_id`, and Seller import `task_id`; embeds the subject
master, locked SKU, and Ozon references; and writes it through `FsRepo`.

- [ ] **Step 4: Run the package-emission test**

Expected: PASS.

### Task 5: Remove image upload controls from the active workbench

**Files:**
- Modify: `src/ozon_v2/workbench/local_server.py`
- Test: `tests/test_workbench_local_server.py`

- [ ] **Step 1: Write failing page-contract tests**

Assert the upload page does not contain:

```text
图片门禁 (Image Gate)
Ozon 公网图片通道
准备直接上传图片
```

Assert it contains:

```text
单张锁定原图建品
商品提交后输出图片任务包
```

Also assert the active stage navigation no longer links to `/images`.

- [ ] **Step 2: Run the page tests**

Expected: FAIL because the current page still exposes the generated-image gate,
R2 settings, and image-stage link.

- [ ] **Step 3: Update page copy and controls**

Remove active image-stage navigation, public-media configuration, generated-image
gate rendering, and final-image counts from upload readiness. Redirect the old
image workspace URL to the upload page while retaining legacy data/file APIs for
diagnostics.

- [ ] **Step 4: Run the page tests**

Expected: PASS.

### Task 6: Publish the Skill inbox contract

**Files:**
- Modify: `skills/ozon-image-generation-controller/SKILL.md`
- Modify: `skills/ozon-product-media-generator/SKILL.md`
- Modify: `skills/ozon-image-generation-controller/agents/openai.yaml`
- Modify: `skills/ozon-product-media-generator/agents/openai.yaml`
- Test: `tests/test_ozon_image_controller_skill.py`
- Test: `tests/test_image_worker_contract_files.py`

- [ ] **Step 1: Write failing contract assertions**

Assert the active Skill documents the fixed pending directory, direct Ozon image
replacement, and the prohibition on returning generated files to the workbench.

- [ ] **Step 2: Run contract tests**

Expected: FAIL with missing inbox/direct-upload text.

- [ ] **Step 3: Update the Skill contracts**

Document periodic pickup, package claiming, product-ID resolution, complete
eight-image replacement, retry receipts outside workbench data, and no workbench
callback. Do not add real credentials or execute an upload.

- [ ] **Step 4: Run contract tests**

Expected: PASS.

### Task 7: Verify the isolated change

**Files:**
- Test: `tests/test_workbench_local_server.py`
- Test: `tests/test_seller_api.py`
- Test: `tests/test_ozon_image_controller_skill.py`
- Test: `tests/test_image_worker_contract_files.py`

- [ ] **Step 1: Run targeted tests**

Run:

```powershell
python -m pytest tests/test_workbench_local_server.py tests/test_seller_api.py tests/test_ozon_image_controller_skill.py tests/test_image_worker_contract_files.py -q
```

Expected: all pass without network calls.

- [ ] **Step 2: Run the complete suite with an extended timeout**

Run:
`python -m pytest -q`

Expected: all tests pass. No real collection, generation, R2 publication, product
upload, or picture upload occurs.

- [ ] **Step 3: Inspect the diff**

Run:

```powershell
git status --short
git diff --check
```

Expected: no whitespace errors and no unrelated user changes removed.
