from daily_brief.cli import build_parser


def test_parser_defaults_to_generate_command():
    parser = build_parser()

    args = parser.parse_args([])

    assert args.command == "generate"
    assert args.output_dir == "briefs"
    assert args.data_dir == "data"
    assert args.dry_run is False
