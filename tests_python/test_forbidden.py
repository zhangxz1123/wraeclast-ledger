from __future__ import annotations

import unittest

from poe_advisor.forbidden import (
    enrich_forbidden_assets,
    forbidden_price_points,
    parse_passive_map,
)


class ForbiddenMetadataTests(unittest.TestCase):
    def test_official_tree_mapping_excludes_cross_ascendancy_name_collision(
        self,
    ) -> None:
        mapping = parse_passive_map(
            {
                "classes": [
                    {
                        "name": "Witch",
                        "ascendancies": [
                            {
                                "id": "Elementalist",
                                "name": "Elementalist",
                            }
                        ],
                    },
                    {
                        "name": "Ranger",
                        "ascendancies": [
                            {"id": "Raider", "name": "Warden"}
                        ],
                    },
                ],
                "nodes": {
                    "1": {
                        "skill": 1,
                        "name": "Shaper of Storms",
                        "isNotable": True,
                        "ascendancyName": "Elementalist",
                    },
                    "2": {
                        "skill": 2,
                        "name": "Shared Name",
                        "isNotable": True,
                        "ascendancyName": "Elementalist",
                    },
                    "3": {
                        "skill": 3,
                        "name": "Shared Name",
                        "isNotable": True,
                        "ascendancyName": "Raider",
                    },
                    "5": {
                        "skill": 5,
                        "name": "Avatar of the Wilds",
                        "isNotable": True,
                        "ascendancyName": "Raider",
                    },
                    "4": {
                        "skill": 4,
                        "name": "Elemental Damage",
                        "isNotable": False,
                        "ascendancyName": "Elementalist",
                    },
                },
            }
        )

        self.assertEqual(
            mapping.passives["shaper of storms"]["ascendancy"],
            "Elementalist",
        )
        self.assertEqual(
            mapping.passives["shaper of storms"]["baseClass"],
            "Witch",
        )
        self.assertNotIn("shared name", mapping.passives)
        self.assertIn("shared name", mapping.ambiguous_names)
        self.assertNotIn("elemental damage", mapping.passives)
        self.assertEqual(
            mapping.passives["avatar of the wilds"]["ascendancy"],
            "Warden",
        )
        self.assertEqual(
            mapping.passives["avatar of the wilds"]["baseClass"],
            "Ranger",
        )

    def test_exact_variant_price_keeps_full_name_and_metadata(self) -> None:
        assets = [
            {
                "source_item_id": "56327",
                "item_key": "currency:divine-orb",
                "name": "Divine Orb",
                "category": "Currency",
                "current_chaos": 200,
                "current_divine": 1,
            },
            {
                "source_item_id": "9001",
                "item_key": (
                    "forbiddenjewel:forbidden-flesh-shaper-of-storms-"
                    "variant-forbidden-flesh"
                ),
                "name": "Shaper of Storms",
                "category": "ForbiddenJewel",
                "current_chaos": 300,
                "current_divine": None,
                "current_daily": 20,
                "low_confidence": False,
                "variant": {
                    "source_name": (
                        "Forbidden Flesh (Shaper of Storms)"
                    ),
                    "variant": "Forbidden Flesh",
                    "passiveName": "Shaper of Storms",
                },
            },
        ]
        mapping = parse_passive_map(
            {
                "classes": [
                    {
                        "name": "Witch",
                        "ascendancies": [
                            {
                                "id": "Elementalist",
                                "name": "Elementalist",
                            }
                        ],
                    }
                ],
                "nodes": {
                    "42": {
                        "skill": 42,
                        "name": "Shaper of Storms",
                        "isNotable": True,
                        "ascendancyName": "Elementalist",
                    }
                },
            }
        )
        coverage = enrich_forbidden_assets(
            assets,
            mapping,
            mapping_snapshot_id=7,
            mapping_endpoint="fixture://official-tree",
        )
        points = forbidden_price_points(
            assets,
            league_id="Fixture",
            observed_at="2026-07-30T06:00:00Z",
            snapshot_id=8,
        )

        self.assertEqual(coverage["mapped"], 1)
        self.assertEqual(len(points), 1)
        self.assertEqual(
            points[0].name,
            "Forbidden Flesh (Shaper of Storms)",
        )
        self.assertAlmostEqual(points[0].divine_value, 1.5)
        self.assertEqual(
            points[0].details["metadata"]["ascendancy"],
            "Elementalist",
        )
        self.assertEqual(
            points[0].details["metadata"]["baseClass"],
            "Witch",
        )


if __name__ == "__main__":
    unittest.main()
