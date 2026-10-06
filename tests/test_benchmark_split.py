import copy
import hashlib
import json
import random

import pytest

from evog.core.errors import ContractError
from evog.core.io import dumps
from evog.evaluation.data import Episode, read_episode_ids
from evog.evaluation.split import create_manifest, validate_manifest, write_manifest


def cohort(counts=(213, 249, 300, 402, 427, 268, 176, 169, 196)):
    return [
        Episode(
            benchmark="evermembench",
            episode_id=f"kind-{kind}-question-{index:04d}",
            question="PRIVATE_QUESTION_CONTENT",
            gold="PRIVATE_REFERENCE_ANSWER",
            question_type=f"kind-{kind}",
            scope=f"scope-{index % 4}",
        )
        for kind, count in enumerate(counts)
        for index in range(count)
    ]


def test_paper_sized_cohort_exact_stratification_and_isolation():
    episodes = cohort()
    manifest = create_manifest(episodes)
    validate_manifest(manifest, episodes)
    assert manifest["counts"]["total"] == 2400
    assert manifest["counts"]["evolution"] == 720
    assert manifest["counts"]["held_out"] == 1680
    assert [v["evolution"] for v in manifest["counts"]["by_type"].values()] == [
        64,
        75,
        90,
        120,
        128,
        80,
        53,
        51,
        59,
    ]
    left, right = set(manifest["evolution_ids"]), set(manifest["held_out_ids"])
    assert not left & right
    assert left | right == {episode.episode_id for episode in episodes}
    assert len(left) == 720 and len(right) == 1680
    serialized = json.dumps(manifest)
    assert "PRIVATE_QUESTION_CONTENT" not in serialized
    assert "PRIVATE_REFERENCE_ANSWER" not in serialized
    assert '"gold"' not in serialized and '"question"' not in serialized


def test_input_order_does_not_change_seeded_split():
    episodes = cohort((13, 17, 20))
    expected = create_manifest(episodes, evolution_size=15, seed=41)
    random.Random(9).shuffle(episodes)
    assert create_manifest(episodes, evolution_size=15, seed=41) == expected
    assert create_manifest(episodes, evolution_size=15, seed=42) != expected


def test_small_cohort_and_equal_remainders():
    manifest = create_manifest(cohort((1, 1, 1)), evolution_size=2)
    assert [v["evolution"] for v in manifest["counts"]["by_type"].values()] == [1, 1, 0]


@pytest.mark.parametrize("field", ["question", "gold", "options", "asking_user_id", "scope"])
def test_every_private_episode_field_affects_source_fingerprint(field):
    episodes = cohort((4, 4))
    original = create_manifest(episodes, evolution_size=2)
    changed = episodes.copy()
    changed[0] = changed[0].model_copy(
        update={field: {"A": "changed option"} if field == "options" else "changed"}
    )
    revised = create_manifest(changed, evolution_size=2)
    assert revised["source_fingerprint"] != original["source_fingerprint"]
    with pytest.raises(ContractError):
        validate_manifest(original, changed)


@pytest.mark.parametrize("mutation", ["membership", "count", "type", "scope", "schema", "extra"])
def test_recomputed_fingerprint_does_not_hide_tampering(mutation):
    episodes = cohort((4, 4))
    manifest = create_manifest(episodes, evolution_size=2)
    tampered = copy.deepcopy(manifest)
    if mutation == "membership":
        tampered["evolution_ids"][0], tampered["held_out_ids"][0] = (
            tampered["held_out_ids"][0],
            tampered["evolution_ids"][0],
        )
    elif mutation == "count":
        tampered["counts"]["by_type"]["kind-0"]["evolution"] += 1
    elif mutation in {"type", "scope"}:
        tampered["episodes"][0]["question_type" if mutation == "type" else "scope"] = "edited"
    elif mutation == "schema":
        tampered["version"] = True
    else:
        tampered["question"] = "PRIVATE_CONTENT"
    tampered.pop("manifest_fingerprint")
    tampered["manifest_fingerprint"] = hashlib.sha256(dumps(tampered).encode()).hexdigest()
    with pytest.raises(ContractError):
        validate_manifest(tampered, episodes)


def test_outputs_are_validated_reusable_private_free_and_never_overwritten(tmp_path):
    episodes = cohort((4, 4))
    manifest = create_manifest(episodes, evolution_size=2)
    paths = write_manifest(manifest, tmp_path, episodes)
    assert json.loads(paths["manifest.json"].read_text()) == manifest
    assert read_episode_ids(paths["evolution.txt"]) == set(manifest["evolution_ids"])
    assert read_episode_ids(paths["held_out.txt"]) == set(manifest["held_out_ids"])
    before = {name: path.read_bytes() for name, path in paths.items()}
    with pytest.raises(ContractError):
        write_manifest(manifest, tmp_path, episodes)
    assert before == {name: path.read_bytes() for name, path in paths.items()}
    assert all(b"PRIVATE_" not in content for content in before.values())


def test_existing_output_is_not_partially_replaced(tmp_path):
    (tmp_path / "held_out.txt").write_text("keep this\n")
    episodes = cohort((4, 4))
    with pytest.raises(ContractError):
        write_manifest(create_manifest(episodes, evolution_size=2), tmp_path, episodes)
    assert (tmp_path / "held_out.txt").read_text() == "keep this\n"
    assert not (tmp_path / "manifest.json").exists()


def test_rejects_duplicate_ids_and_unreadable_id_lists():
    episodes = cohort((2,))
    with pytest.raises(ContractError):
        create_manifest([episodes[0], episodes[0]], evolution_size=1)
    for value in ["#ignored", "id\nextra", " id", "id "]:
        changed = [episodes[0].model_copy(update={"episode_id": value}), episodes[1]]
        with pytest.raises(ContractError):
            create_manifest(changed, evolution_size=1)


@pytest.mark.parametrize("size,seed", [(0, 0), (9, 0), (True, 0), (1, True), (1, "0")])
def test_rejects_invalid_size_or_seed(size, seed):
    with pytest.raises(ContractError):
        create_manifest(cohort((4, 4)), evolution_size=size, seed=seed)
