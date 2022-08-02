"""
Contains message used by Schemas paginated CLI.
"""


def construct_cli_display_message_for_schemas(page_to_render, last_page_number=None):
    if last_page_number is None:
        last_page_number = "many"
    single_page = "Event Schemas"
    first_page = f"Event Schemas [Page {page_to_render}/{last_page_number}] (Enter N for next page)"

    middle_page = f"Event Schemas [Page {page_to_render}/{last_page_number}] (Enter N/P for next/previous page)"

    last_page = f"Event Schemas [Page {page_to_render}/{last_page_number}] (Enter P for previous page)"

    return {"single_page": single_page, "first_page": first_page, "middle_page": middle_page, "last_page": last_page}


def construct_cli_display_message_for_registries(page_to_render, last_page_number=None):
    if last_page_number is None:
        last_page_number = "many"
    single_page = "Schema Registry"
    first_page = f"Schema Registry [Page {page_to_render}/{last_page_number}] (Enter N for next page)"

    middle_page = f"Schema Registry [Page {page_to_render}/{last_page_number}] (Enter N/P for next/previous page)"

    last_page = f"Schema Registry [Page {page_to_render}/{last_page_number}] (Enter P for previous page)"

    return {"single_page": single_page, "first_page": first_page, "middle_page": middle_page, "last_page": last_page}
