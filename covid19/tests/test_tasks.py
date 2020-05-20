from unittest.mock import Mock, patch

from django.test import TestCase
from model_bakery import baker

from covid19.exceptions import OnlyOneSpreadsheetException
from covid19.models import StateSpreadsheet
from covid19.notifications import notify_import_success
from covid19.tasks import process_new_spreadsheet_task


class ProcessNewSpreadsheetTaskTests(TestCase):
    def test_function_is_a_task(self):
        assert getattr(process_new_spreadsheet_task, "delay", None)

    @patch.object(
        StateSpreadsheet,
        "link_to_matching_spreadsheet_peer",
        Mock(side_effect=OnlyOneSpreadsheetException, autospec=True),
    )
    @patch("covid19.tasks.notify_new_spreadsheet", autospec=True)
    @patch.object(StateSpreadsheet, "import_to_final_dataset", Mock())
    def test_dispatch_new_spreadsheet_notification_if_nothing_to_compare_with(self, mock_notify):
        spreadsheet = baker.make(StateSpreadsheet)

        process_new_spreadsheet_task(spreadsheet.id)

        spreadsheet.link_to_matching_spreadsheet_peer.assert_called_once_with()
        mock_notify.assert_called_once_with(spreadsheet)
        assert not spreadsheet.import_to_final_dataset.called

    @patch.object(
        StateSpreadsheet, "link_to_matching_spreadsheet_peer", Mock(return_value=(False, ["errors"]), autospec=True)
    )
    @patch("covid19.tasks.notify_spreadsheet_mismatch", autospec=True)
    def test_dispatch_mismatch_notification_if_errors(self, mock_notify):
        spreadsheet = baker.make(StateSpreadsheet)

        process_new_spreadsheet_task(spreadsheet.id)

        spreadsheet.link_to_matching_spreadsheet_peer.assert_called_once_with()
        mock_notify.assert_called_once_with(spreadsheet, ["errors"])

    @patch.object(StateSpreadsheet, "link_to_matching_spreadsheet_peer", Mock(return_value=(True, []), autospec=True))
    @patch.object(StateSpreadsheet, "import_to_final_dataset", Mock(return_value=(True, []), autospec=True))
    def test_import_data_if_spreadsheet_is_ready(self):
        spreadsheet = baker.make(StateSpreadsheet)

        process_new_spreadsheet_task(spreadsheet.id)

        spreadsheet.link_to_matching_spreadsheet_peer.assert_called_once_with()
        spreadsheet.import_to_final_dataset.assert_called_once_with(notify_import_success)

    @patch.object(StateSpreadsheet, "link_to_matching_spreadsheet_peer", Mock())
    def test_do_not_process_spreadsheet_if_for_some_reason_it_is_cancelled(self):
        spreadsheet = baker.make(StateSpreadsheet, cancelled=True)
        assert not spreadsheet.link_to_matching_spreadsheet_peer.called
