"""Creates the policy and the twin graph of the print farm in Eclipse Ditto (idempotent).

  python ditto_setup.py                          # farm + printer_01, printer_02
  python ditto_setup.py printer_03               # add one more printer (its 9 twins)
  python ditto_setup.py --reset                  # delete and recreate the twins (state is lost)
"""
import argparse
import logging
import sys

from twin.ditto_client import DittoClient, DittoError
from twin.ditto_graph import farm_thing, policy, printer_graph
from twin.config import DITTO_POLICY_ID, DITTO_URL

log = logging.getLogger("ditto_setup")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("printers", nargs="*", default=["printer_01", "printer_02"])
    p.add_argument("--reset", action="store_true", help="delete existing twins and create them again")
    p.add_argument("--ditto-url", default=DITTO_URL)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    ditto = DittoClient("ditto", url=args.ditto_url)
    ditto.wait_ready()
    ditto.put_policy(DITTO_POLICY_ID, policy())
    log.info("policy %s: entries owner, adapter, monitor", DITTO_POLICY_ID)

    things = [farm_thing()]
    for i, printer in enumerate(args.printers, 1):
        index = int(printer.rsplit("_", 1)[-1]) if printer.rsplit("_", 1)[-1].isdigit() else i
        things += printer_graph(printer, index)

    created = kept = 0
    for thing_id, thing in things:
        if args.reset:
            ditto.delete_thing(thing_id)
        if ditto.get_thing(thing_id, fields="thingId") is not None:
            # keep the live state of existing twins, refresh only the static part (attributes incl. relations)
            ditto.merge_thing(thing_id, {"attributes": thing["attributes"]})
            kept += 1
            continue
        try:
            ditto.put_thing(thing_id, thing)
        except DittoError as e:
            log.error("%s: %s", thing_id, e)
            sys.exit(1)
        created += 1
        log.info("created %-32s %s", thing_id, thing["definition"].rsplit("/", 1)[-1])
    log.info("twins: %d created, %d already existed (attributes refreshed); total in Ditto: %d",
             created, kept, ditto.count())


if __name__ == "__main__":
    main()
