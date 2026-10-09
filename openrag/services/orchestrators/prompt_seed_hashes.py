"""Hashes of bundled query-contextualizer revisions stored as DB defaults.

When the bundled template changes, move its previous current hash into the
superseded set so existing unedited defaults can be refreshed on upgrade.
"""

_CURRENT_SEED_HASHES: dict[str, str] = {
    "query_contextualizer": "a9ab41975b720c6b7718d88d35df69ebddf81f4a298e32c4c474a2b88f6ad463",
}

_SUPERSEDED_SEED_HASHES: dict[str, frozenset[str]] = {
    "query_contextualizer": frozenset(
        {
            "2a950346e27dd815fd42af0849caf0065f615c3bef4bff6d61d1621b39fcd066",
            "db4fbb3ff1d1b91df19a31a872ae83b73a9b63b993f2f20a473d4c87725d19aa",
            "d7b5f06862e57587c6bc2965af4a70dcc5327dfccae8172c32ab02623878f6f6",
            "c1ff41d1a86909ea608e59cc4c50e8cbf831d5368c70b0018c256816f024be0f",
            "8a3b0fc89b47a189b8e696f9383ad1f1f5023759b1ab3e01636c9455694c0b4e",
            "e508d9b8a1c6f46f6e4b8a461c4f064d84b0242fc6e6dbb1136a94a27de79a61",
            "252bc17d09818212282eea72e38e7927dadee85ff50718d490087605c7b34872",
            "bfaaf14620d277726db22903206c85c097de1a3263d7951678ae451c279e3977",
            "931a2e095ccc6443ecba0d2684d8dd32c9ffac6f484d659953f78bc96e229a0a",
            "0867f3358bd431ff625d65eafbd273d65928c84b49f915af6b4dc8a2c3c1f068",
            "0f9b16fb4bde42ba863708c4956647b17735a2c4fe245b0ba0c6a106a56bf4dd",
            "87f5084a4cc922681730969ed98e769550a973bc4464f45e65b5342a7cfbf91e",
            "4377989d16e8e40b8849b723ab9e181293360928569ef7bbd3b66ce964c0fe9b",
        }
    ),
}
