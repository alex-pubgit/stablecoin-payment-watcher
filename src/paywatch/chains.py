"""Chain and token presets.

Every token address below was read back from the chain on 2026-11-18 (symbol, decimals)
with `paywatch verify-presets`, and the README states which ones were also compared with
the issuer's own published list. Check them yourself before relying on any payments tool.

The RPC URLs are keyless public endpoints that answered the calls this tool makes when
they were tested. They are rate-limited and fine for trying the tool. Put your own
provider's URLs in the config file for real use.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from paywatch.evm import normalize_address


class ConfigError(Exception):
    """The configuration is wrong. The message names the entry and the fix."""


@dataclass(frozen=True, slots=True)
class Token:
    symbol: str
    address: str
    decimals: int


@dataclass(frozen=True, slots=True)
class Chain:
    key: str
    name: str
    chain_id: int
    # Blocks on top of a transfer before it is reported. Defaults, not finality guarantees.
    confirmations: int
    rpc_urls: tuple[str, ...]
    explorer: str
    tokens: dict[str, Token] = field(default_factory=dict)

    def token(self, symbol: str) -> Token:
        try:
            return self.tokens[symbol.upper()]
        except KeyError:
            known = ", ".join(sorted(self.tokens)) or "none"
            raise ConfigError(
                f"Token '{symbol}' is not configured on {self.name} (known: {known}). "
                "Add it under [[token]] in the config file with its contract address."
            ) from None

    def tx_url(self, tx_hash: str) -> str:
        return f"{self.explorer}/tx/{tx_hash}"

    def with_token(self, token: Token) -> Chain:
        return replace(self, tokens={**self.tokens, token.symbol.upper(): token})


def _token(symbol: str, address: str, decimals: int) -> Token:
    return Token(symbol, normalize_address(address), decimals)


CHAINS: dict[str, Chain] = {
    "bsc": Chain(
        key="bsc",
        name="BNB Smart Chain",
        chain_id=56,
        confirmations=15,
        rpc_urls=("https://bsc-rpc.publicnode.com", "https://1rpc.io/bnb"),
        explorer="https://bscscan.com",
        tokens={
            # Binance-Peg tokens, issued by Binance. Note the 18 decimals.
            "USDT": _token("USDT", "0x55d398326f99059fF775485246999027B3197955", 18),
            "USDC": _token("USDC", "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d", 18),
        },
    ),
    "ethereum": Chain(
        key="ethereum",
        name="Ethereum",
        chain_id=1,
        confirmations=12,
        rpc_urls=("https://ethereum-rpc.publicnode.com", "https://1rpc.io/eth"),
        explorer="https://etherscan.io",
        tokens={
            "USDT": _token("USDT", "0xdAC17F958D2ee523a2206206994597C13D831ec7", 6),
            "USDC": _token("USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6),
        },
    ),
    "polygon": Chain(
        key="polygon",
        name="Polygon",
        chain_id=137,
        confirmations=30,
        rpc_urls=("https://polygon-bor-rpc.publicnode.com",),
        explorer="https://polygonscan.com",
        tokens={
            # No USDT preset: the contract usually used for it on Polygon now reports the
            # symbol USDT0, and Tether's own list gives no Polygon address. See the README.
            "USDC": _token("USDC", "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", 6),
        },
    ),
}


def chain(key: str, chains: dict[str, Chain] | None = None) -> Chain:
    chains = CHAINS if chains is None else chains
    try:
        return chains[key.lower()]
    except KeyError:
        raise ConfigError(
            f"Unknown chain '{key}'. Known chains: {', '.join(sorted(chains))}."
        ) from None
