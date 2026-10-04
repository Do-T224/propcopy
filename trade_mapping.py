"""Persistent ticket mapping between master and slave positions."""

import json
import logging
import os

logger = logging.getLogger("trade_mapping")


class TradeMapping:
    def __init__(self, mapping_file: str = "data/trade_mapping.json"):
        self.mapping_file = mapping_file
        self.mapping: dict[str, dict[str, int]] = {}
        self._load()

    def add(self, master_ticket: int, slave_account: int, slave_ticket: int) -> None:
        key = str(master_ticket)
        if key not in self.mapping:
            self.mapping[key] = {}
        self.mapping[key][str(slave_account)] = slave_ticket
        self._save()
        logger.info(f"Mapped master {master_ticket} -> slave {slave_account}:{slave_ticket}")

    def remove(self, master_ticket: int, slave_account: int | None = None) -> None:
        key = str(master_ticket)
        if key not in self.mapping:
            return
        if slave_account is not None:
            self.mapping[key].pop(str(slave_account), None)
            if not self.mapping[key]:
                del self.mapping[key]
        else:
            del self.mapping[key]
        self._save()

    def get_slave_ticket(self, master_ticket: int, slave_account: int) -> int | None:
        return self.mapping.get(str(master_ticket), {}).get(str(slave_account))

    def get_all_slave_tickets(self, master_ticket: int) -> dict[str, int]:
        return self.mapping.get(str(master_ticket), {})

    def get_all(self) -> dict:
        """Return full mapping: {master_ticket: [{slave_account, slave_ticket}, ...], ...}"""
        result = {}
        for master_key, slaves in self.mapping.items():
            result[master_key] = [
                {"slave_account": int(sa), "slave_ticket": st}
                for sa, st in slaves.items()
            ]
        return result

    def get_all_master_tickets(self) -> list[int]:
        return [int(k) for k in self.mapping]

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.mapping_file), exist_ok=True)
        tmp = self.mapping_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.mapping, f, indent=2)
        os.replace(tmp, self.mapping_file)

    def _load(self) -> None:
        if not os.path.exists(self.mapping_file):
            return
        try:
            with open(self.mapping_file, "r") as f:
                self.mapping = json.load(f)
            logger.info(f"Loaded {len(self.mapping)} master ticket mappings from disk")
        except (json.JSONDecodeError, KeyError):
            logger.warning("Corrupt mapping file - starting fresh")
            self.mapping = {}
