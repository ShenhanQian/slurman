from argparse import ArgumentParser

from . import run_ui


def slurman_cli():
    parser = ArgumentParser("SLURM UI")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "-i",
        "--interval",
        help="Specify the interval in seconds to refresh the UI",
        type=int,
        default=10,
    )
    parser.add_argument(
        "-r",
        "--history-range",
        help="Specify the time range of history jobs to load",
        type=str,
        default="1 week",
    )
    args = parser.parse_args()
    run_ui(
        verbose=args.verbose,
        interval=args.interval,
        history_range=args.history_range,
    )

