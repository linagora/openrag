"""Released prompt history used to safely refresh unchanged database defaults.

Each history is ordered from the oldest released seed to the bundled template
in this revision. Earlier hashes are refreshable; the final hash is the trusted
current template. When changing a template, append its new hash once. Keeping
the prior value in place preserves the upgrade history without a separate
current-hash and superseded-hash edit.
"""

# Canonical prompt type -> (PromptsConfig key, bundled template filename).
# Keep the image-captioning config alias here so prompt seeding and the
# standard-library history check share one mapping.
_PROMPT_TEMPLATE_METADATA: dict[str, tuple[str, str]] = {
    "sys_prompt": ("sys_prompt", "sys_prompt_tmpl.txt"),
    "query_contextualizer": ("query_contextualizer", "query_contextualizer_tmpl.txt"),
    "chunk_contextualizer": ("chunk_contextualizer", "chunk_contextualizer_tmpl.txt"),
    "image_captioning": ("image_describer", "image_captioning_tmpl.txt"),
    "spoken_style_answer": ("spoken_style_answer", "spoken_style_answer_tmpl.txt"),
    "topic_tagger": ("topic_tagger", "topic_tagger_tmpl.txt"),
    "asr_transcription": ("asr_transcription", "asr_transcription_tmpl.txt"),
    "agent": ("agent", "agent_tmpl.txt"),
}

# Per-type hashes, oldest stable release first and the bundled template last.
# Keep only texts that a stable release could have seeded, plus the current
# bundled hash. The comments identify where each distinct text shipped.
_PROMPT_SEED_HASH_HISTORY: dict[str, tuple[str, ...]] = {
    "query_contextualizer": (
        "931a2e095ccc6443ecba0d2684d8dd32c9ffac6f484d659953f78bc96e229a0a",  # v2.1.0-v2.2.1
        "8a3b0fc89b47a189b8e696f9383ad1f1f5023759b1ab3e01636c9455694c0b4e",  # v2.2.2
        "d7b5f06862e57587c6bc2965af4a70dcc5327dfccae8172c32ab02623878f6f6",  # v2.3.0-v2.3.1
        "2a950346e27dd815fd42af0849caf0065f615c3bef4bff6d61d1621b39fcd066",  # v2.3.2
        "6dd246ecb0cd47cfe8ccb707e015dbcf3b70882592088b37e3a9f12e5954516e",  # date_order, unreleased
        "521f7ceb3f5ffc366ccd0c2dbbe93ae2912a5b37f4be24cb7ae7f457b4d6c287",  # current (a refining keyword rewrites the previous question)
    ),
    "spoken_style_answer": (
        "36d3df218283f91de2ca62693afcdd9b844ba5645fba929a98c7b7ddd0d320b1",  # v2.1.0-v2.1.1
        "1d99bca86e8dce884ac1a50d0071577eafe26b383d568225aa7f9b7ee4a02c67",  # v2.2.0-v2.3.2
        "02c53ce3b00ffd72bada72134d47bb14b31ae4724a9ce03455a221c361733261",  # current (provenance line)
    ),
    "sys_prompt": (
        "b5faacb19c176961965882bdf2f30c6dec7973de26c26c7bf18dd8f9088c07a7",  # v2.1.0-v2.1.1
        "3f0e6c8d2e385eda15715c83f54d701cfe83fe4ffddc2cbfd6ba398ac1c599dd",  # v2.2.0-v2.3.2
        "e8668be88af50f77e1d6b2f52407c20341de0bb65647185c2f4a88c1dcef0410",  # provenance line, unreleased
        "93cc6fa1c1c4982d1190a32504f3058c88c28b9c680c29ee24c1831ab15a5794",  # current (a refining keyword asks the previous question)
    ),
    "asr_transcription": (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",  # v2.2.0-v2.3.2 and current
    ),
    "agent": (
        "20f9b236caf361d2603da086aa97c93d3150021c1c84c2594a875f4366aad198",  # current (agentic mode)
    ),
    "chunk_contextualizer": (
        "35cc087891c587627f572d4876890c3a78a59e3141b217d05ffd77750ffd1af6",  # v2.1.0-v2.3.2 and current
    ),
    "image_captioning": (
        "0f6ea8060787d5d1c9d75243d9d2f1055b7852a2720fe226f6f0008ba2604056",  # v2.1.0-v2.3.2 and current
    ),
    "topic_tagger": (
        "5d9965a1b3f84ab3000e2a68b9cbc78f579dfd49d2119ebf1547810f3b33bd0e",  # v2.1.0-v2.3.2 and current
    ),
}
