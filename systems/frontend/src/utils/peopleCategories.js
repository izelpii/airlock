// ─── People categories as tabs ───────────────────────────────────────────────
//
// The Tag Face picker shows one tab per people category rather than everybody
// on one page, which stopped scaling as the people list grew.
//
// The tabs are the categories table plus any category a person carries that
// it lacks - nobody may end up on no tab at all - with "Other" for anybody
// without one, which is what creating a person without a category records.
// Family comes first, as it is where the picker opens; the rest follow
// alphabetically.

export const DEFAULT_CATEGORY = 'Family';

export const categoryOf = (person) => person.category || 'Other';

export function categoryTabs(categories, people) {
  const names = new Set(categories || []);
  (people || []).forEach((p) => names.add(categoryOf(p)));
  return [...names].sort((a, b) => {
    if (a === DEFAULT_CATEGORY) return -1;
    if (b === DEFAULT_CATEGORY) return 1;
    return a.localeCompare(b);
  });
}

export function countByCategory(people) {
  return (people || []).reduce((m, p) => {
    m[categoryOf(p)] = (m[categoryOf(p)] || 0) + 1;
    return m;
  }, {});
}

// The tab to open on: Family when there is one, else the first.
export const openingTab = (tabs) =>
  (tabs.includes(DEFAULT_CATEGORY) ? DEFAULT_CATEGORY : (tabs[0] ?? DEFAULT_CATEGORY));
