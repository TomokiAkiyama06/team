"""料金テーブル（config/pricing.toml）と推定費用の計算。

ここで計算する金額はあくまでボット側の推定値であり、実際の請求額とは一致しない場合がある。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

MTOK = 1_000_000


class PricingError(Exception):
    pass


@dataclass(frozen=True)
class ModelPrice:
    input: float          # USD / 1M input tokens
    output: float         # USD / 1M output tokens
    cache_write: float    # USD / 1M cache write tokens (5分TTL)
    cache_read: float     # USD / 1M cache read tokens


class PriceTable:
    def __init__(self, prices: dict[str, ModelPrice]):
        if not prices:
            raise PricingError("料金テーブルが空です")
        self.prices = prices

    @classmethod
    def load(cls, path: Path | str) -> PriceTable:
        path = Path(path)
        if not path.exists():
            raise PricingError(f"料金テーブルが見つかりません: {path}")
        with path.open("rb") as f:
            data = tomllib.load(f)
        prices: dict[str, ModelPrice] = {}
        for model, p in data.get("models", {}).items():
            try:
                prices[model] = ModelPrice(
                    input=float(p["input"]), output=float(p["output"]),
                    cache_write=float(p["cache_write"]), cache_read=float(p["cache_read"]),
                )
            except (KeyError, TypeError, ValueError) as e:
                raise PricingError(f"料金テーブルの {model} の形式が不正です: {e}") from e
        return cls(prices)

    def has(self, model: str) -> bool:
        return model in self.prices

    def price_for(self, model: str) -> ModelPrice:
        """設定モデルの料金。未登録なら PricingError（起動時に検出して停止させる）。"""
        try:
            return self.prices[model]
        except KeyError:
            raise PricingError(
                f"モデル {model} の料金が {', '.join(sorted(self.prices))} に含まれていません。"
                " config/pricing.toml に追加してください。") from None

    def price_or_conservative(self, model: str | None) -> ModelPrice:
        """フォールバック等で想定外のモデルが応答した場合は最も高い料金で計上する。"""
        if model and model in self.prices:
            return self.prices[model]
        return self.conservative()

    def conservative(self) -> ModelPrice:
        ps = self.prices.values()
        return ModelPrice(
            input=max(p.input for p in ps), output=max(p.output for p in ps),
            cache_write=max(p.cache_write for p in ps), cache_read=max(p.cache_read for p in ps),
        )


def cost_usd(price: ModelPrice, input_tokens: int, output_tokens: int,
             cache_creation_input_tokens: int = 0, cache_read_input_tokens: int = 0) -> float:
    return (input_tokens * price.input
            + output_tokens * price.output
            + cache_creation_input_tokens * price.cache_write
            + cache_read_input_tokens * price.cache_read) / MTOK
