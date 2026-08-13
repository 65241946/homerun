from __future__ import annotations

from typing import Any


DEFAULT_GLOBAL_RISK = {
    "max_gross_exposure_usd": 5000.0,
    "max_daily_loss_usd": 500.0,
    "max_orders_per_cycle": 50,
}


def _normalize_template(template: dict[str, Any]) -> dict[str, Any]:
    configs = []
    for source_config in template.get("source_configs", []) or []:
        if not isinstance(source_config, dict):
            continue
        normalized_source_config = dict(source_config)
        normalized_source_config["strategy_key"] = str(source_config.get("strategy_key") or "").strip().lower()
        configs.append(normalized_source_config)

    out = dict(template)
    out["source_configs"] = configs
    return out


TRADER_TEMPLATES: list[dict[str, Any]] = [
    # Was ``btc_eth_full_stack``: three ``crypto`` source_configs in one
    # trader, described as "equivalent to the legacy multi-mode 'auto'
    # trader".  A trader now runs exactly one strategy per source_key, and
    # ``_normalize_source_configs`` enforces that by raising on a duplicate
    # source_key.  The template was never migrated, so it raised — and
    # because ``list_trader_templates`` normalizes every template to build
    # its response, that one bad entry took the whole
    # ``GET /traders/templates`` endpoint down with it, not just itself.
    # The maker and directional halves already had single-strategy
    # templates; convergence did not, so this is that missing third
    # preset, carrying the convergence params the bundle held.
    {
        "id": "btc_eth_convergence_only",
        "name": "Crypto Convergence Trader",
        "description": "BTC/ETH convergence-only execution with oracle direction gating and near-expiry entry windows.",
        "source_configs": [
            {
                "source_key": "crypto",
                "strategy_key": "btc_eth_convergence",
                "strategy_params": {
                    "min_edge_percent": 1.8,
                    "min_confidence": 0.42,
                    "max_open_order_seconds": 8.0,
                    "include_assets": ["BTC", "ETH"],
                    "exclude_assets": ["SOL", "XRP"],
                    "include_timeframes": ["5m", "15m"],
                    "exclude_timeframes": ["1h", "4h"],
                    "oracle_direction_gate_modes": ["convergence"],
                    "max_market_data_age_ms_5m": 3000,
                    "max_market_data_age_ms_15m": 3200,
                    "min_seconds_left_for_entry_5m": 35.0,
                    "min_seconds_left_for_entry_15m": 60.0,
                },
            }
        ],
        "interval_seconds": 1,
        "risk_limits": {
            "max_open_orders": 8,
            "max_per_market_exposure_usd": 400.0,
            "max_entry_drift_pct": 35.0,
        },
    },
    {
        "id": "btc_eth_directional_only",
        "name": "Crypto Directional Trader",
        "description": "BTC/ETH directional-only execution with strict oracle and freshness gates.",
        "source_configs": [
            {
                "source_key": "crypto",
                "strategy_key": "btc_eth_directional_edge",
                "strategy_params": {
                    "min_edge_percent": 2.8,
                    "min_confidence": 0.48,
                    "max_open_order_seconds": 6.0,
                    "timeout_taker_rescue_enabled": True,
                    "timeout_taker_rescue_price_bps": 25.0,
                    "timeout_taker_rescue_time_in_force": "IOC",
                    "include_assets": ["BTC", "ETH"],
                    "include_timeframes": ["5m", "15m"],
                    "exclude_timeframes": ["1h", "4h"],
                    "require_oracle_for_directional": True,
                    "oracle_direction_gate_modes": ["directional"],
                    "orderflow_alignment_enabled": True,
                    "orderflow_alignment_modes": ["directional"],
                    "edge_calibration_enabled": True,
                },
            }
        ],
        "interval_seconds": 1,
        "risk_limits": {
            "max_open_orders": 6,
            "max_per_market_exposure_usd": 350.0,
        },
    },
    {
        "id": "btc_eth_maker_only",
        "name": "Crypto Maker Trader",
        "description": "BTC/ETH maker-only stack with cancellation-cluster and orderflow quality gates.",
        "source_configs": [
            {
                "source_key": "crypto",
                "strategy_key": "btc_eth_maker_quote",
                "strategy_params": {
                    "min_edge_percent": 1.8,
                    "min_confidence": 0.38,
                    "max_open_order_seconds": 7.0,
                    "timeout_taker_rescue_enabled": True,
                    "timeout_taker_rescue_price_bps": 20.0,
                    "timeout_taker_rescue_time_in_force": "IOC",
                    "include_assets": ["BTC", "ETH"],
                    "include_timeframes": ["5m", "15m"],
                    "orderflow_alignment_enabled": True,
                    "orderflow_alignment_modes": ["maker_quote"],
                    "min_orderflow_alignment": 0.05,
                    "cancel_cluster_guard_enabled": True,
                    "cancel_cluster_guard_modes": ["maker_quote"],
                    "max_cancel_rate_30s": 0.70,
                    "edge_calibration_enabled": True,
                },
            }
        ],
        "interval_seconds": 1,
        "risk_limits": {
            "max_open_orders": 8,
            "max_per_market_exposure_usd": 300.0,
        },
    },
    {
        "id": "news_edge",
        "name": "News Trader",
        "description": "News-event reaction strategy on validated news intents.",
        "source_configs": [
            {
                "source_key": "news",
                "strategy_key": "news_edge",
                "strategy_params": {
                    "min_edge_percent": 8.0,
                    "min_confidence": 0.55,
                },
            }
        ],
        "interval_seconds": 120,
        "risk_limits": {
            "max_open_orders": 6,
            "max_per_market_exposure_usd": 300.0,
        },
    },
    # Was one ``scanner_weather`` template bundling a scanner config and a
    # weather config.  ``_validate_source_configs`` requires exactly one
    # entry — a trader runs one strategy on one source — so the bundle was
    # rejected by ``create_trader`` and the preset could never be used.
    # Split into the two traders it always described, params unchanged.
    {
        "id": "scanner_basic",
        "name": "Scanner Trader",
        "description": "Scanner executor on the basic opportunity strategy.",
        "source_configs": [
            {
                "source_key": "scanner",
                "strategy_key": "basic",
                "strategy_params": {
                    "min_edge_percent": 4.0,
                    "min_confidence": 0.45,
                    "max_risk_score": 0.78,
                    "min_liquidity": 25.0,
                },
            }
        ],
        "interval_seconds": 120,
        "risk_limits": {
            "max_open_orders": 10,
            "max_per_market_exposure_usd": 350.0,
        },
    },
    {
        "id": "weather_distribution",
        "name": "Weather Trader",
        "description": "Weather executor with model-agreement and source-spread gates.",
        "source_configs": [
            {
                "source_key": "weather",
                "strategy_key": "weather_distribution",
                "strategy_params": {
                    "min_edge_percent": 6.0,
                    "min_confidence": 0.58,
                    "min_model_agreement": 0.62,
                    "min_source_count": 2,
                    "max_source_spread_c": 4.0,
                },
            }
        ],
        "interval_seconds": 120,
        "risk_limits": {
            "max_open_orders": 10,
            "max_per_market_exposure_usd": 350.0,
        },
    },
    {
        "id": "flash_tape_ported",
        "name": "Flash Reversion Stack",
        "description": "Scanner execution tuned for flash-reversion and tail-carry opportunities.",
        "source_configs": [
            {
                "source_key": "scanner",
                "strategy_key": "flash_crash_reversion",
                "strategy_params": {
                    "min_edge_percent": 3.0,
                    "min_confidence": 0.4,
                    "max_risk_score": 0.8,
                    "min_liquidity": 1500.0,
                    "min_abs_move_5m": 1.5,
                    "sizing_policy": "kelly",
                    "kelly_fractional_scale": 0.5,
                },
            }
        ],
        "interval_seconds": 60,
        "risk_limits": {
            "max_open_orders": 8,
            "max_per_market_exposure_usd": 300.0,
        },
    },
    {
        "id": "news_momentum_breakout",
        "name": "News Momentum Breakout",
        "description": "Ride fresh upward breakouts on general (non-crypto, non-sports) markets with scale-out take-profits.",
        "source_configs": [
            {
                "source_key": "scanner",
                "strategy_key": "news_momentum_breakout",
                "strategy_params": {
                    "min_edge_percent": 4.0,
                    "min_confidence": 0.40,
                    "max_risk_score": 0.78,
                    "min_liquidity": 3000.0,
                    "breakout_threshold": 0.10,
                    "min_entry_price": 0.18,
                    "max_entry_price": 0.78,
                    "max_spread": 0.06,
                    "max_retrace_from_peak_fraction": 0.35,
                    "min_5m_share_of_30m": 0.45,
                    "max_abs_move_2h_pct": 80.0,
                    "require_breakout_shape": True,
                    "require_breakout_alignment": True,
                    "min_abs_move_5m": 4.0,
                    "emit_cooldown_seconds": 300,
                    "exclude_crypto_markets": True,
                    "exclude_sports_markets": True,
                    "sizing_policy": "kelly",
                    "kelly_fractional_scale": 0.4,
                    "take_profit_pct": 70.0,
                    "stop_loss_pct": 25.0,
                    "trailing_stop_pct": 12.0,
                    "trailing_stop_activation_profit_pct": 25.0,
                    "max_hold_minutes": 240,
                    "momentum_stall_minutes": 45,
                },
            }
        ],
        "interval_seconds": 30,
        "risk_limits": {
            "max_open_orders": 6,
            "max_per_market_exposure_usd": 250.0,
        },
    },
    {
        "id": "traders_copy_trade",
        "name": "Traders Copy Trade",
        "description": "Real-time explicit copy trading strategy with wallet-scope controls.",
        "source_configs": [
            {
                "source_key": "traders",
                "strategy_key": "traders_copy_trade",
                "strategy_params": {
                    "min_confidence": 0.45,
                    "min_source_notional_usd": 15.0,
                    "max_signal_age_seconds": 5,
                    "copy_delay_seconds": 0,
                    "copy_buys": True,
                    "copy_sells": True,
                    "max_position_size": 500.0,
                    "proportional_sizing": True,
                    "proportional_multiplier": 1.0,
                    "traders_scope": {
                        "modes": ["tracked", "pool"],
                        "individual_wallets": [],
                        "group_ids": [],
                    },
                },
            }
        ],
        "interval_seconds": 5,
        "risk_limits": {
            "max_open_orders": 30,
            "max_per_market_exposure_usd": 600.0,
        },
    },
    {
        "id": "manual_manage_hold",
        "name": "Manual Manage Hold",
        "description": "Manage adopted live wallet positions without opening new entries.",
        "source_configs": [
            {
                "source_key": "manual",
                "strategy_key": "manual_wallet_position",
                "strategy_params": {},
            }
        ],
        "interval_seconds": 30,
        "risk_limits": {
            "max_open_orders": 20,
            "max_per_market_exposure_usd": 1000.0,
        },
    },
]


def get_template(template_id: str) -> dict[str, Any] | None:
    key = str(template_id or "").strip().lower()
    for template in TRADER_TEMPLATES:
        if template["id"] == key:
            return _normalize_template(template)
    return None
