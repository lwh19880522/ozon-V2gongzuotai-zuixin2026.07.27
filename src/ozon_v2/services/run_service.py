from __future__ import annotations

import random
from collections import Counter
from typing import Any

from ozon_v2.adapters.credential_prompt import CredentialPromptLauncher
from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.app.result import Result
from ozon_v2.services.credential_service import CredentialService
from ozon_v2.domain.models import CollectionPair, QueryGenerationStatus, RunStatus, SeedProduct
from ozon_v2.domain.policies import (
    decide_ozon_candidate_dedupe,
    decide_seed_existing_product_dedupe,
    seed_has_generated_ozon_query,
)
from ozon_v2.domain.validators import validate_collection_pair


class RunService:
    def __init__(self, repo: FsRepo | None = None, credential_prompt_launcher: CredentialPromptLauncher | None = None) -> None:
        self.repo = repo or FsRepo()
        self.credential_prompt_launcher = credential_prompt_launcher or CredentialPromptLauncher(self.repo)

    def doctor(self) -> Result:
        self.repo.initialize_runtime()
        active_seeds = self.repo.load_active_seeds()
        existing_products = self.repo.load_existing_products()
        credentials = self.repo.credential_status().to_dict()
        dedupe_status = self.repo.existing_store_dedupe_status()
        full_store_dedupe_ready = credentials["configured"] and dedupe_status["ready"]
        return Result.success(
            "doctor.ok",
            "Ozon V2 paths and seed assets are ready.",
            {
                "project_root": str(self.repo.context.project_root),
                "runtime_root": str(self.repo.runtime_root),
                "initial_seed_asset": str(self.repo.context.paths.initial_seed_json),
                "active_seed_count": len(active_seeds),
                "used_seed_count": len(self.repo.load_used_seed_ids()),
                "existing_store_dedupe_count": len(existing_products),
                "existing_store_dedupe_refreshed_at": dedupe_status["refreshed_at"],
                "existing_store_dedupe_meta_path": dedupe_status["meta_path"],
                "seller_credentials_configured": credentials["configured"],
                "seller_credentials_path": credentials["credentials_path"],
                "seller_credentials_template_path": credentials["template_path"],
                "seller_client_id": credentials["client_id"],
                "seller_api_key_masked": credentials["api_key_masked"],
                "full_store_dedupe_ready": full_store_dedupe_ready,
            },
        )

    def start_run(self, target_count: int, random_seed: int | None = None) -> Result:
        if target_count <= 0:
            return Result.failure("run.invalid_target_count", "target_count must be greater than zero.")
        self.repo.initialize_runtime()
        credential_status = CredentialService(self.repo).status().data
        if not credential_status["configured"]:
            assistant_launch = self.credential_prompt_launcher.open_or_focus()
            credential_status["credential_assistant"] = assistant_launch.to_dict()
            return Result.failure(
                "run.credentials_missing",
                "Seller credentials are required before starting a full store-deduped run. Credential assistant was requested.",
                data=credential_status,
            )
        dedupe_status = self.repo.existing_store_dedupe_status()
        if not dedupe_status["ready"]:
            return Result.failure(
                "run.existing_store_dedupe_missing",
                "Existing store dedupe must be refreshed before seed sampling.",
                data=dedupe_status,
            )
        existing_products = self.repo.load_existing_products()
        used_seed_ids = self.repo.load_used_seed_ids()
        active_seeds = self.repo.load_active_seeds()
        eligible: list[SeedProduct] = []
        blocked: list[dict[str, Any]] = []
        for seed in active_seeds:
            if seed.seed_id in used_seed_ids:
                blocked.append({"seed_id": seed.seed_id, "reason": "already used"})
                continue
            decision = decide_seed_existing_product_dedupe(seed, existing_products)
            if decision.kind.value != "clear":
                blocked.append({"seed_id": seed.seed_id, "reason": decision.reason})
                continue
            eligible.append(seed)
        if len(eligible) < target_count:
            return Result.failure(
                "run.insufficient_seeds",
                "Not enough eligible seeds after existing-store dedupe filtering.",
                data={"eligible_seed_count": len(eligible), "target_count": target_count, "blocked": blocked},
            )
        selected_random_seed = random_seed if random_seed is not None else random.SystemRandom().randint(1, 2**31 - 1)
        sampled = self.repo.sample_seeds(eligible, target_count, selected_random_seed)
        status = RunStatus.READY_FOR_OZON_COLLECTION if all(seed_has_generated_ozon_query(seed) for seed in sampled) else RunStatus.NEEDS_QUERY_GENERATION
        run = self.repo.create_run_record(target_count, sampled, selected_random_seed, status)
        return Result.success(
            "run.started",
            "Run created from eligible active seeds.",
            {
                "run": run,
                "sampled_seeds": [seed.to_dict() for seed in sampled],
                "blocked_seed_count": len(blocked),
                "next_action": "generate_seed_queries" if status == RunStatus.NEEDS_QUERY_GENERATION else "create_ozon_collection_contract",
            },
        )

    def attach_seed_queries(self, run_id: str, queries_by_seed_id: dict[str, list[str]], method: str = "manual_mapping") -> Result:
        seeds = self.repo.load_sampled_seeds(run_id)
        missing: list[str] = []
        for seed in seeds:
            terms = queries_by_seed_id.get(seed.seed_id)
            if not terms:
                missing.append(seed.seed_id)
                continue
            seed.ozon_query_terms_ru = terms
            seed.query_generation_status = QueryGenerationStatus.GENERATED
            seed.query_generation_method = method
            seed.query_generation_confidence = "high"
        if missing:
            self.repo.save_sampled_seeds(run_id, seeds)
            return Result.failure(
                "query.missing_for_sampled_seed",
                "Every sampled seed needs generated Ozon query terms before Ozon collection.",
                data={"missing_seed_ids": missing},
            )
        self.repo.save_sampled_seeds(run_id, seeds)
        run = self.repo.load_run(run_id)
        run["status"] = RunStatus.READY_FOR_OZON_COLLECTION.value
        self.repo.save_run(run)
        return Result.success("query.attached", "Generated Ozon query terms attached to sampled seeds.", {"run_id": run_id})

    def status(self, run_id: str | None = None) -> Result:
        self.repo.initialize_runtime()
        if not run_id:
            return Result.success(
                "status.ready",
                "Ozon V2 runtime status.",
                {
                    "active_seed_count": len(self.repo.load_active_seeds()),
                    "used_seed_count": len(self.repo.load_used_seed_ids()),
                    "existing_store_dedupe_count": len(self.repo.load_existing_products()),
                },
            )
        run = self.repo.load_run(run_id)
        return Result.success("status.run", "Ozon V2 run status.", {"run": run})

    def next_action(self, run_id: str) -> Result:
        run = self.repo.load_run(run_id)
        seeds = self.repo.load_sampled_seeds(run_id)
        if not all(seed_has_generated_ozon_query(seed) for seed in seeds):
            return Result.success(
                "next.generate_seed_queries",
                "Generated Russian-first Ozon query terms are required before Ozon collection.",
                {"run_id": run_id, "seeds": [seed.to_dict() for seed in seeds]},
            )
        run["status"] = RunStatus.READY_FOR_OZON_COLLECTION.value
        self.repo.save_run(run)
        return Result.success(
            "next.ozon_collection_contract",
            "Run is ready for an Ozon collection task contract.",
            {"run_id": run_id, "sampled_seed_count": len(seeds)},
        )

    def replace_sampled_seed(
        self,
        run_id: str,
        rejected_seed_id: str,
        reason: str,
        random_seed: int | None = None,
    ) -> Result:
        sampled_seeds = self.repo.load_sampled_seeds(run_id)
        rejected_seed = next((seed for seed in sampled_seeds if seed.seed_id == rejected_seed_id), None)
        if rejected_seed is None:
            return Result.failure(
                "seed_replace.not_sampled",
                "Rejected seed_id must belong to the current sampled seed list.",
                data={"run_id": run_id, "rejected_seed_id": rejected_seed_id},
            )
        used_seed_ids = self.repo.load_used_seed_ids()
        rejected_attempt_ids = {item.get("seed_id") for item in self.repo.load_rejected_seed_attempts(run_id)}
        sampled_ids = {seed.seed_id for seed in sampled_seeds}
        excluded_ids = used_seed_ids | rejected_attempt_ids | sampled_ids
        existing_products = self.repo.load_existing_products()
        eligible: list[SeedProduct] = []
        blocked: list[dict[str, Any]] = []
        for seed in self.repo.load_active_seeds():
            if seed.seed_id in excluded_ids:
                blocked.append({"seed_id": seed.seed_id, "reason": "already sampled, rejected, or used"})
                continue
            decision = decide_seed_existing_product_dedupe(seed, existing_products)
            if decision.kind.value != "clear":
                blocked.append({"seed_id": seed.seed_id, "reason": decision.reason})
                continue
            eligible.append(seed)
        if not eligible:
            return Result.failure(
                "seed_replace.no_eligible_replacement",
                "No eligible seed is available for replacement after dedupe filtering.",
                data={"run_id": run_id, "blocked_seed_count": len(blocked)},
            )
        selected_random_seed = random_seed if random_seed is not None else random.SystemRandom().randint(1, 2**31 - 1)
        replacement_seed = self.repo.sample_seeds(eligible, 1, selected_random_seed)[0]
        replacement_index = next(index for index, seed in enumerate(sampled_seeds) if seed.seed_id == rejected_seed_id)
        sampled_seeds[replacement_index] = replacement_seed
        self.repo.save_sampled_seeds(run_id, sampled_seeds)
        self.repo.append_rejected_seed_attempt(run_id, rejected_seed, reason, replacement_seed.seed_id)
        run = self.repo.load_run(run_id)
        run["sampled_seed_ids"] = [seed.seed_id for seed in sampled_seeds]
        run["rejected_seed_ids"] = [item.get("seed_id") for item in self.repo.load_rejected_seed_attempts(run_id)]
        run["status"] = (
            RunStatus.READY_FOR_OZON_COLLECTION.value
            if all(seed_has_generated_ozon_query(seed) for seed in sampled_seeds)
            else RunStatus.NEEDS_QUERY_GENERATION.value
        )
        self.repo.save_run(run)
        return Result.success(
            "seed_replace.replaced",
            "Rejected sampled seed was recorded and replaced with a fresh eligible seed.",
            {
                "run": run,
                "rejected_seed": rejected_seed.to_dict(),
                "replacement_seed": replacement_seed.to_dict(),
                "blocked_seed_count": len(blocked),
            },
        )

    def ingest_collection_output(self, run_id: str, payload: dict[str, Any]) -> Result:
        sampled = {seed.seed_id: seed for seed in self.repo.load_sampled_seeds(run_id)}
        existing_products = self.repo.load_existing_products()
        pairs_payload = payload.get("collection_pairs", [])
        pairs: list[CollectionPair] = []
        errors: list[str] = []
        for item in pairs_payload:
            pair = CollectionPair.from_dict(item)
            if pair.seed_product.seed_id not in sampled:
                errors.append(f"pair {pair.pair_id} seed_id is not in sampled seed list")
            dedupe_decision = decide_ozon_candidate_dedupe(pair.ozon_candidate, existing_products)
            if dedupe_decision.kind.value == "duplicate":
                errors.append(f"pair {pair.pair_id}: Ozon candidate matches existing store product")
            if dedupe_decision.kind.value == "possible_duplicate":
                errors.append(f"pair {pair.pair_id}: Ozon candidate needs manual review for possible duplicate")
            pair_errors = validate_collection_pair(pair)
            errors.extend([f"pair {pair.pair_id}: {error}" for error in pair_errors])
            pairs.append(pair)
        sampled_ids = set(sampled)
        pair_seed_ids = [pair.seed_product.seed_id for pair in pairs]
        if len(pairs) != len(sampled):
            errors.append(f"collection output must include exactly {len(sampled)} pairs; got {len(pairs)}")
        missing_seed_ids = sorted(sampled_ids - set(pair_seed_ids))
        if missing_seed_ids:
            errors.append(f"collection output missing sampled seed_ids: {', '.join(missing_seed_ids)}")
        duplicate_seed_ids = sorted(seed_id for seed_id, count in Counter(pair_seed_ids).items() if count > 1)
        if duplicate_seed_ids:
            errors.append(f"collection output has duplicate seed_ids: {', '.join(duplicate_seed_ids)}")
        if errors:
            return Result.failure("ingest.invalid_collection_output", "Collection output failed validation.", errors=errors)
        self.repo.append_collection_pairs(run_id, pairs)
        evidence_path = self.repo.write_evidence_csv(run_id, pairs)
        self.finalize_run(run_id)
        return Result.success(
            "ingest.accepted",
            "Collection output ingested and run finalized.",
            {"run_id": run_id, "pair_count": len(pairs), "evidence_csv": str(evidence_path)},
        )

    def finalize_run(self, run_id: str) -> Result:
        sampled_seeds = self.repo.load_sampled_seeds(run_id)
        rejected_attempts = self.repo.load_rejected_seed_attempts(run_id)
        rejected_seeds = [SeedProduct.from_dict(item["seed"]) for item in rejected_attempts if item.get("seed")]
        removed = self.repo.remove_active_seeds({seed.seed_id for seed in sampled_seeds + rejected_seeds})
        self.repo.append_used_seeds(run_id, sampled_seeds, "sampled")
        if rejected_seeds:
            self.repo.append_used_seeds(run_id, rejected_seeds, "rejected_collection_attempt")
        run = self.repo.load_run(run_id)
        run["status"] = RunStatus.FINALIZED.value
        run["removed_seed_ids"] = [seed.seed_id for seed in removed]
        run["rejected_seed_ids"] = [seed.seed_id for seed in rejected_seeds]
        self.repo.save_run(run)
        return Result.success(
            "run.finalized",
            "Run finalized and sampled seeds removed from active pool.",
            {"run_id": run_id, "removed_seed_count": len(removed)},
        )
