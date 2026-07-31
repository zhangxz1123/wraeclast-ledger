from __future__ import annotations

import unittest

from poe_advisor.normalization import (
    canonical_key,
    normalize_ggg_markets,
    normalize_poe_ninja,
    slugify,
)


CHAOS_ID = "Metadata/Items/Currency/CurrencyRerollRare"
DIVINE_ID = "Metadata/Items/Currency/CurrencyModValues"


def market(
    item_id: str,
    quote_id: str,
    item_amount: float,
    quote_amount: float,
    *,
    league: str = "Fixture League",
    volume: float = 400,
) -> dict:
    return {
        "league": league,
        "market_pair": [item_id, quote_id],
        "market_id": f"{item_id}|{quote_id}",
        "lowest_ratio": {item_id: item_amount, quote_id: quote_amount},
        "highest_ratio": {
            item_id: item_amount * 2,
            quote_id: quote_amount * 2,
        },
        "volume_traded": {item_id: volume, quote_id: volume * quote_amount},
    }


class PoeNinjaNormalizationTests(unittest.TestCase):
    def test_exchange_overview_uses_primary_rates_and_preserves_trend_metadata(
        self,
    ) -> None:
        payload = {
            "core": {
                "primary": "chaos",
                "rates": {"chaos": 1, "divine": 0.005},
                "items": {
                    "veiled-orb": {
                        "id": "veiled-orb",
                        "name": "Veiled Orb",
                        "detailsId": "veiled-orb",
                    }
                },
            },
            "lines": [
                {
                    "id": "veiled-orb",
                    "primaryValue": 40,
                    "volumePrimaryValue": 2500,
                    "pay": {"listing_count": 80},
                    "receive": {"listing_count": 120, "count": 1000},
                    "sparkline": {
                        "data": [1.0, 2.5, None],
                        "totalChange": 2.5,
                    },
                }
            ],
        }

        points = normalize_poe_ninja(
            payload,
            league_id="Fixture League",
            category="Currency",
            observed_at="2026-07-29T12:00:00Z",
            snapshot_id=7,
        )

        self.assertEqual(len(points), 1)
        point = points[0]
        self.assertEqual(point.name, "Veiled Orb")
        self.assertEqual(point.item_key, "currency:veiled-orb")
        self.assertAlmostEqual(point.divine_value, 0.2)
        self.assertAlmostEqual(point.chaos_value, 40.0)
        self.assertEqual(point.listing_count, 120)
        self.assertEqual(point.volume, 2500.0)
        self.assertEqual(point.snapshot_id, 7)
        self.assertEqual(
            point.details["relative_trend_samples"],
            [1.0, 2.5, None],
        )
        self.assertEqual(point.details["relative_total_change_pct"], 2.5)
        self.assertGreater(point.confidence, 0.2)
        self.assertLessEqual(point.confidence, 0.98)

    def test_stash_item_schema_prefers_direct_values_and_variant_identity(
        self,
    ) -> None:
        payload = {
            "lines": [
                {
                    "name": "Awakened Added Fire Damage Support",
                    "detailsId": "awakened-added-fire-damage-support",
                    "divineValue": 1.25,
                    "chaosValue": 250,
                    "listingCount": "321",
                    "gemLevel": 5,
                    "gemQuality": 20,
                    "corrupted": False,
                    "category": "SkillGem",
                },
                {
                    "name": "Invalid price",
                    "detailsId": "invalid",
                    "divineValue": float("nan"),
                },
            ]
        }

        points = normalize_poe_ninja(
            payload,
            league_id="Fixture League",
            category="SkillGem",
            observed_at="2026-07-29T12:00:00Z",
            snapshot_id=8,
        )

        self.assertEqual(len(points), 1)
        point = points[0]
        self.assertEqual(point.divine_value, 1.25)
        self.assertEqual(point.chaos_value, 250.0)
        self.assertEqual(point.listing_count, 321)
        self.assertEqual(point.category, "SkillGem")
        self.assertIn("gemlevel-5", point.item_key)
        self.assertIn("gemquality-20", point.item_key)
        self.assertEqual(point.details["gemLevel"], 5)
        self.assertEqual(point.details["gemQuality"], 20)

    def test_invalid_poe_ninja_shape_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "lines array"):
            normalize_poe_ninja(
                {"lines": None},
                league_id="Fixture League",
                category="Currency",
                observed_at="2026-07-29T12:00:00Z",
                snapshot_id=1,
            )


class OfficialExchangeNormalizationTests(unittest.TestCase):
    def test_cross_rate_and_supported_identifier_mappings(self) -> None:
        identifiers = {
            "Metadata/Items/Currency/CurrencyGemQuality": (
                "Gemcutter's Prism",
                "Currency",
            ),
            "Metadata/Items/Currency/CurrencyDelveCraftingCold": (
                "Frigid Fossil",
                "Fossil",
            ),
            "Metadata/Items/Currency/CurrencyDelveCraftingMana": (
                "Lucent Fossil",
                "Fossil",
            ),
            "Metadata/Items/Currency/CurrencyEssenceHorror1": (
                "Essence of Horror",
                "Essence",
            ),
            "Metadata/Items/Currency/CurrencyEssenceContempt7": (
                "Deafening Essence of Contempt",
                "Essence",
            ),
            "Metadata/Items/Currency/CurrencyEldritchEmber4": (
                "Exceptional Eldritch Ember",
                "Currency",
            ),
            "Metadata/Items/Currency/CurrencyLegionFragmentMaraketh": (
                "Timeless Maraketh Emblem",
                "Fragment",
            ),
            "Metadata/Items/Currency/CurrencyValdoPuzzleBox": (
                "Valdo's Puzzle Box",
                "Fragment",
            ),
        }
        markets = [
            market(CHAOS_ID, DIVINE_ID, 200, 1, volume=100_000),
            market(
                "Metadata/Items/Currency/CurrencyDelveCraftingCold",
                CHAOS_ID,
                2,
                10,
                volume=400,
            ),
        ]
        markets.extend(
            market(identifier, DIVINE_ID, 4, 1, volume=800)
            for identifier in identifiers
            if not identifier.endswith("CurrencyDelveCraftingCold")
        )
        markets.append(market("ignored", DIVINE_ID, 1, 9, league="Other League"))
        markets.append(
            market(
                "Metadata/Items/Deepwater/DeepwaterBottledItem",
                DIVINE_ID,
                8,
                1,
            )
        )

        points = normalize_ggg_markets(
            {"markets": markets},
            league_id="Fixture League",
            league_name="Fixture League",
            observed_at="2026-07-29T12:00:00Z",
            snapshot_id=42,
        )
        by_name = {point.name: point for point in points}

        for expected_name, expected_category in identifiers.values():
            with self.subTest(expected_name=expected_name):
                self.assertIn(expected_name, by_name)
                self.assertEqual(
                    by_name[expected_name].category,
                    expected_category,
                )
                self.assertEqual(
                    by_name[expected_name].source,
                    "ggg-currency-exchange",
                )
                self.assertEqual(by_name[expected_name].snapshot_id, 42)
                ninja_point = normalize_poe_ninja(
                    {
                        "lines": [
                            {
                                "id": slugify(expected_name),
                                "name": expected_name,
                                "detailsId": slugify(expected_name),
                                "divineValue": 0.25,
                            }
                        ]
                    },
                    league_id="Fixture League",
                    category=expected_category,
                    observed_at="2026-07-29T12:15:00Z",
                    snapshot_id=43,
                )[0]
                self.assertEqual(
                    by_name[expected_name].item_key,
                    ninja_point.item_key,
                )

        fossil = by_name["Frigid Fossil"]
        self.assertAlmostEqual(fossil.chaos_value, 5.0)
        self.assertAlmostEqual(fossil.divine_value, 0.025)
        self.assertEqual(fossil.volume, 400.0)
        self.assertAlmostEqual(
            by_name["Gemcutter's Prism"].divine_value,
            0.25,
        )
        self.assertFalse(
            by_name["Deepwater Bottled Item"].details["identifier_resolved"]
        )
        self.assertNotIn("Ignored", by_name)

    def test_market_id_fallback_and_invalid_shape(self) -> None:
        payload = {
            "markets": [
                {
                    "league": "Fixture League",
                    "market_id": f"{CHAOS_ID}|{DIVINE_ID}",
                    "lowest_ratio": {CHAOS_ID: 200, DIVINE_ID: 1},
                    "highest_ratio": {CHAOS_ID: 400, DIVINE_ID: 2},
                    "volume_traded": {CHAOS_ID: 50_000},
                }
            ]
        }
        points = normalize_ggg_markets(
            payload,
            league_id="fixture-id",
            league_name="Fixture League",
            observed_at="2026-07-29T12:00:00Z",
            snapshot_id=2,
        )
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].name, "Chaos Orb")
        self.assertAlmostEqual(points[0].divine_value, 0.005)

        with self.assertRaisesRegex(ValueError, "contain markets"):
            normalize_ggg_markets(
                [],
                league_id="fixture-id",
                league_name="Fixture League",
                observed_at="2026-07-29T12:00:00Z",
                snapshot_id=2,
            )

    def test_key_helpers_are_stable(self) -> None:
        self.assertEqual(slugify("Gemcutter's Prism"), "gemcutters-prism")
        self.assertEqual(
            canonical_key("Chaos Orb", "Currency", "chaos"),
            "currency:chaos",
        )
        self.assertEqual(
            canonical_key("Maven's Writ", "Fragment", "mavens-writ"),
            "currency:mavens-writ",
        )


if __name__ == "__main__":
    unittest.main()
