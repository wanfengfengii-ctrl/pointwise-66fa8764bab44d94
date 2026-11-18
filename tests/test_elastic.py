import json
import sqlite3
from pathlib import Path

from device.clock import tick
from device.elastic import load_elastic_trigger
from device.journal import read_cursor
from device.receive import receive
from elastic_sample import check_expected_receipts, run_sample
from handover.publish import publish
from inspection.judgment import Verdict
from inspection.receipts import receipt
from sampling.planner import book, book_elastic
from shift.calibration import Calibration
from shift.register import register, register_elastic
from tests.support import WorkspaceTest, deny_commit


SAMPLE_PATH = Path(__file__).resolve().parent.parent / 'elastic-example.json'


class ElasticSampleTests(WorkspaceTest):
    def setUp(self):
        super().setUp()
        self.sample = json.loads(SAMPLE_PATH.read_text(encoding='utf-8'))

    def test_sample_swap_out_of_order_signing_and_reopen(self):
        commands, receipts = run_sample(self.db, self.sample, self.device)
        actual = check_expected_receipts(self.sample, commands, receipts)
        self.assertEqual(actual, [list(row) for row in self.sample['expected_receipts']])
        # The three exposures are signed to B, C, A in original-beat order.
        self.assertEqual([receipts[beat].ticket_id for beat in (1, 2, 3)], ['B', 'C', 'A'])
        self.assertEqual([commands[beat].trigger_id for beat in (1, 2, 3)], [1, 2, 3])
        self.assertEqual(list(self.device.commands.values()), [
            {'trigger_id': 1, 'shift_id': 'CC-elastic', 'probe': 'P', 'beat': 1},
            {'trigger_id': 2, 'shift_id': 'CC-elastic', 'probe': 'P', 'beat': 2},
            {'trigger_id': 3, 'shift_id': 'CC-elastic', 'probe': 'P', 'beat': 3},
        ])
        # Signing order fixes receipt numbers: beat 2, then beat 1, then beat 3.
        self.assertEqual([receipts[beat].receipt_id for beat in (1, 2, 3)], [2, 1, 3])
        self.reopen()
        for beat in (1, 2, 3):
            self.assertEqual(receipt(self.db, receipts[beat].receipt_id), receipts[beat])
        self.assertEqual(receive(self.db, commands[1].trigger_id, 101), receipts[1])


class ElasticRegistrationTests(WorkspaceTest):
    def test_replay_and_mode_conflicts(self):
        self.assertEqual(
            register_elastic(self.db, 'el', {1: ['P'], 2: ['P']}, {'P': Calibration(2, 3)}),
            'el',
        )
        # Same shift, same configuration, same mode: replay keeps the registration.
        self.assertEqual(
            register_elastic(self.db, 'el', {2: ['P'], 1: ['P']}, {'P': Calibration(2, 3)}),
            'el',
        )
        with self.assertRaises(ValueError):
            register_elastic(self.db, 'el', {1: ['P']}, {'P': Calibration(2, 3)})
        with self.assertRaises(ValueError):
            register(self.db, 'el', {1: ['P'], 2: ['P']}, {'P': Calibration(2, 3)})
        register(self.db, 'fx', {1: ['P']}, {'P': Calibration(2, 3)})
        with self.assertRaises(ValueError):
            register_elastic(self.db, 'fx', {1: ['P']}, {'P': Calibration(2, 3)})
        # The fixed entries reject elastic shifts and vice versa.
        with self.assertRaises(ValueError):
            book(self.db, 'el', 't1', 1, 'P', 100)
        with self.assertRaises(ValueError):
            book_elastic(self.db, 'fx', 't2', 1, 1, 'P', 100)
        with self.assertRaises(ValueError):
            publish(self.db, 'el')
        with self.assertRaises(LookupError):
            book_elastic(self.db, 'missing', 't3', 1, 1, 'P', 100)
        # The fixed shift still books and publishes.
        self.assertEqual(book(self.db, 'fx', 't4', 1, 'P', 100), 't4')
        self.assertIsInstance(publish(self.db, 'fx'), int)


class ElasticBookingTests(WorkspaceTest):
    def test_invalid_bookings_are_rejected(self):
        register_elastic(self.db, 'el', {1: ['P'], 2: ['P'], 3: ['P']},
                         {'P': Calibration(2, 3)})
        for args in (
            ('t', 3, 2, 'P', 100),      # empty window
            ('t', 0, 1, 'P', 100),      # nonpositive beat
            ('t', -1, 2, 'P', 100),     # negative beat
            ('t', 1, 2, 'P', True),     # boolean target
            ('t', True, 2, 'P', 100),   # boolean beat
            ('t', 1, 2, 'Q', 100),      # uncalibrated probe
            ('', 1, 2, 'P', 100),       # empty ticket number
            ('t', 1, 2, '', 100),       # empty probe
        ):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    book_elastic(self.db, 'el', *args)
        self.assertEqual(book_elastic(self.db, 'el', 'ok', 1, 3, 'P', 100), 'ok')
        with self.assertRaises(ValueError):
            book_elastic(self.db, 'el', 'ok', 1, 3, 'P', 100)

    def test_ticket_numbers_are_unique_across_modes(self):
        register(self.db, 'fx', {1: ['P']}, {'P': Calibration(2, 3)})
        register_elastic(self.db, 'el', {1: ['P']}, {'P': Calibration(2, 3)})
        book(self.db, 'fx', 'shared', 1, 'P', 100)
        with self.assertRaises(ValueError):
            book_elastic(self.db, 'el', 'shared', 1, 1, 'P', 100)
        book_elastic(self.db, 'el', 'other', 1, 1, 'P', 100)
        register(self.db, 'fx2', {2: ['P']}, {'P': Calibration(2, 3)})
        with self.assertRaises(ValueError):
            book(self.db, 'fx2', 'other', 2, 'P', 100)

    def test_window_must_stay_fulfillable(self):
        register_elastic(self.db, 'el', {1: ['P'], 2: ['Q'], 3: ['P']},
                         {'P': Calibration(2, 3), 'Q': Calibration(-1, 2)})
        self.assertIsNone(tick(self.db, 'el', 1))
        with self.assertRaises(ValueError):
            book_elastic(self.db, 'el', 'late', 1, 1, 'P', 100)
        with self.assertRaises(ValueError):
            book_elastic(self.db, 'el', 'probeless', 2, 2, 'P', 100)
        self.assertEqual(book_elastic(self.db, 'el', 'fine', 2, 3, 'P', 100), 'fine')

    def test_failed_booking_leaves_no_ticket(self):
        register_elastic(self.db, 'el', {1: ['P']}, {'P': Calibration(2, 3)})
        with deny_commit(self.db), self.assertRaisesRegex(sqlite3.DatabaseError, 'not authorized'):
            book_elastic(self.db, 'el', 'A', 1, 1, 'P', 100)
        self.assertEqual(self.db.execute('SELECT count(*) FROM elastic_tickets').fetchone(), (0,))
        self.assertEqual(book_elastic(self.db, 'el', 'A', 1, 1, 'P', 100), 'A')


class ElasticDispatchTests(WorkspaceTest):
    def test_second_beat_passes_empty_while_first_reply_is_held(self):
        register_elastic(self.db, 'el', {1: ['P'], 2: ['P']}, {'P': Calibration(2, 3)})
        book_elastic(self.db, 'el', 'A', 1, 2, 'P', 100)
        first = self.device.advance(self.db, 'el', 1)
        self.assertIsNotNone(first)
        self.assertIsNone(self.device.advance(self.db, 'el', 2))
        result = self.device.release(self.db, first.trigger_id, 101)
        self.assertEqual((result.ticket_id, result.beat), ('A', 1))

    def test_shared_beat_dispatches_the_probe_that_keeps_arrangement(self):
        register_elastic(self.db, 'el', {1: ['P'], 2: ['P', 'Q']},
                         {'P': Calibration(2, 3), 'Q': Calibration(-1, 2)})
        book_elastic(self.db, 'el', 'A', 1, 2, 'P', 100)
        book_elastic(self.db, 'el', 'B', 2, 2, 'Q', 100)
        first = self.device.advance(self.db, 'el', 1)
        self.assertEqual(first.probe, 'P')
        second = self.device.advance(self.db, 'el', 2)
        self.assertEqual(second.probe, 'Q')
        later = self.device.release(self.db, second.trigger_id, 99)
        self.assertEqual((later.ticket_id, later.measurement.corrected_um, later.verdict),
                         ('B', 98, Verdict.PASS))
        earlier = self.device.release(self.db, first.trigger_id, 101)
        self.assertEqual((earlier.ticket_id, earlier.measurement.deviation_um), ('A', 3))

    def test_one_beat_offers_one_opportunity_even_with_two_probes(self):
        register_elastic(self.db, 'el', {1: ['P', 'Q']},
                         {'P': Calibration(2, 3), 'Q': Calibration(-1, 2)})
        book_elastic(self.db, 'el', 'C', 1, 1, 'P', 100)
        with self.assertRaises(ValueError):
            book_elastic(self.db, 'el', 'D', 1, 1, 'Q', 100)

    def test_reverse_reply_preserves_the_earlier_trigger(self):
        register_elastic(self.db, 'el', {1: ['P'], 2: ['P'], 3: ['P']},
                         {'P': Calibration(2, 3)})
        book_elastic(self.db, 'el', 'A', 1, 2, 'P', 100)
        first = self.device.advance(self.db, 'el', 1)
        book_elastic(self.db, 'el', 'B', 2, 3, 'P', 100)
        second = self.device.advance(self.db, 'el', 2)
        later = self.device.release(self.db, second.trigger_id, 96)
        self.assertEqual(later.ticket_id, 'B')
        earlier = self.device.release(self.db, first.trigger_id, 101)
        self.assertEqual(earlier.ticket_id, 'A')
        # No unsigned work remains: the last beat passes empty.
        self.assertIsNone(self.device.advance(self.db, 'el', 3))

    def test_elastic_tick_order_and_unknown_shift(self):
        register_elastic(self.db, 'el', {2: ['P'], 9: ['P']}, {'P': Calibration(2, 3)})
        with self.assertRaisesRegex(ValueError, 'out of order'):
            tick(self.db, 'el', 9)
        with self.assertRaises(LookupError):
            tick(self.db, 'missing', 2)
        self.assertIsNone(tick(self.db, 'el', 2))
        self.assertIsNone(tick(self.db, 'el', 9))
        with self.assertRaisesRegex(ValueError, 'no more'):
            tick(self.db, 'el', 9)

    def test_failed_tick_keeps_cursor_and_undelivered_command(self):
        register_elastic(self.db, 'el', {1: ['P'], 2: ['P']}, {'P': Calibration(2, 3)})
        book_elastic(self.db, 'el', 'A', 1, 2, 'P', 100)
        with deny_commit(self.db), self.assertRaisesRegex(sqlite3.DatabaseError, 'not authorized'):
            tick(self.db, 'el', 1)
        self.assertEqual(read_cursor(self.db, 'el'), 0)
        self.assertEqual(self.device.commands, {})
        self.assertEqual(self.db.execute('SELECT count(*) FROM elastic_triggers').fetchone(), (0,))
        command = self.device.advance(self.db, 'el', 1)
        self.assertIsNotNone(command)


class ElasticReplyTests(WorkspaceTest):
    def test_replay_conflict_and_unknown_numbers(self):
        register_elastic(self.db, 'el', {1: ['P']}, {'P': Calibration(2, 3)})
        book_elastic(self.db, 'el', 'A', 1, 1, 'P', 100)
        command = self.device.advance(self.db, 'el', 1)
        result = self.device.release(self.db, command.trigger_id, 101)
        self.assertEqual(self.device.release(self.db, command.trigger_id, 101), result)
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            receive(self.db, command.trigger_id, 102)
        with self.assertRaises(LookupError):
            receive(self.db, 999, 101)
        with self.assertRaises(LookupError):
            receipt(self.db, 999)
        with self.assertRaises(ValueError):
            receive(self.db, command.trigger_id, True)
        self.assertEqual(self.db.execute('SELECT count(*) FROM elastic_receipts').fetchone(), (1,))
        self.assertEqual(receipt(self.db, result.receipt_id), result)

    def test_failed_first_signing_preserves_ticket_and_trigger(self):
        register_elastic(self.db, 'el', {1: ['P']}, {'P': Calibration(2, 3)})
        book_elastic(self.db, 'el', 'A', 1, 1, 'P', 100)
        command = self.device.advance(self.db, 'el', 1)
        with deny_commit(self.db), self.assertRaisesRegex(sqlite3.DatabaseError, 'not authorized'):
            receive(self.db, command.trigger_id, 101)
        self.assertIsNone(load_elastic_trigger(self.db, command.trigger_id).receipt_id)
        self.assertEqual(self.db.execute('SELECT count(*) FROM elastic_receipts').fetchone(), (0,))
        self.assertEqual(
            self.db.execute(
                'SELECT count(*) FROM elastic_tickets WHERE receipt_id IS NOT NULL'
            ).fetchone(),
            (0,),
        )
        result = receive(self.db, command.trigger_id, 101)
        self.assertEqual((result.ticket_id, result.measurement.raw_um), ('A', 101))
        self.assertEqual(receive(self.db, command.trigger_id, 101), result)


class SharedNumberSpaceTests(WorkspaceTest):
    def test_triggers_and_receipts_share_one_number_space(self):
        register(self.db, 'fx', {1: ['P'], 2: ['P']}, {'P': Calibration(2, 3)})
        register_elastic(self.db, 'el', {1: ['P'], 2: ['P']}, {'P': Calibration(2, 3)})
        book(self.db, 'fx', 'f1', 1, 'P', 100)
        book(self.db, 'fx', 'f2', 2, 'P', 100)
        book_elastic(self.db, 'el', 'e1', 1, 2, 'P', 100)
        fixed_first = self.device.advance(self.db, 'fx', 1)
        elastic_first = self.device.advance(self.db, 'el', 1)
        fixed_second = self.device.advance(self.db, 'fx', 2)
        self.assertEqual(
            (fixed_first.trigger_id, elastic_first.trigger_id, fixed_second.trigger_id),
            (1, 2, 3),
        )
        elastic_receipt = self.device.release(self.db, elastic_first.trigger_id, 100)
        fixed_receipt = self.device.release(self.db, fixed_first.trigger_id, 101)
        self.assertEqual((elastic_receipt.receipt_id, fixed_receipt.receipt_id), (1, 2))
        self.assertEqual(receipt(self.db, 1), elastic_receipt)
        self.assertEqual(receipt(self.db, 2), fixed_receipt)
        self.assertEqual((elastic_receipt.ticket_id, fixed_receipt.ticket_id), ('e1', 'f1'))
        # The signed elastic ticket is gone; a new trigger would strand: beat passes empty.
        self.assertIsNone(self.device.advance(self.db, 'el', 2))
        later = self.device.release(self.db, fixed_second.trigger_id, 99)
        self.assertEqual(later.receipt_id, 3)
