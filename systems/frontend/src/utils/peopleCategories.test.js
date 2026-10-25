import { categoryTabs, countByCategory, openingTab } from './peopleCategories';

const people = [
  { name: 'Ana', category: 'Family' },
  { name: 'Ben', category: 'Familia Silva Costa' },
  { name: 'Cat', category: null },
  { name: 'Dan', category: 'Neighbours' },          // not in the table
];

test('Family comes first, the rest alphabetically', () => {
  expect(categoryTabs(['Other', 'Familia Silva Costa', 'Family', 'Colleagues'], people))
    .toEqual(['Family', 'Colleagues', 'Familia Silva Costa', 'Neighbours', 'Other']);
});

test('nobody is on no tab: unlisted categories and the uncategorised get one', () => {
  const tabs = categoryTabs(['Family'], people);
  expect(tabs).toContain('Neighbours');
  expect(tabs).toContain('Other');
});

test('each tab counts its people, the uncategorised under Other', () => {
  expect(countByCategory(people)).toEqual(
    { Family: 1, 'Familia Silva Costa': 1, Other: 1, Neighbours: 1 });
});

test('the picker opens on Family, or on the first tab when there is none', () => {
  expect(openingTab(['Family', 'Other'])).toBe('Family');
  expect(openingTab(['Colleagues', 'Other'])).toBe('Colleagues');
  expect(openingTab([])).toBe('Family');
});
