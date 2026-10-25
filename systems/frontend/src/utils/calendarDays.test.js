import { isoOf, linkFor, neighbours } from './calendarDays';

// [year, month, day, photos, videos, in_an_event], newest first
const days = [[2022, 9, 18, 3, 0, 0], [2022, 9, 16, 4402, 128, 4500], [2022, 9, 1, 0, 2, 0]];

test('a day goes to Events when an event holds any of it', () => {
  expect(linkFor(days[1])).toBe('/events?date=2022-09-16');
});

test('otherwise to Photos, or to Videos for a day of videos only', () => {
  expect(linkFor(days[0])).toBe('/photos?date=2022-09-18');
  expect(linkFor(days[2])).toBe('/videos?date=2022-09-01');
});

test('the days with files either side', () => {
  expect(isoOf(days[1])).toBe('2022-09-16');
  expect(neighbours(days, '2022-09-16')).toEqual({ earlier: '2022-09-01', later: '2022-09-18' });
  expect(neighbours(days, '2022-09-18')).toEqual({ earlier: '2022-09-16', later: null });
});
