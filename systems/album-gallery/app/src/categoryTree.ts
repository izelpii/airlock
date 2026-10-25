/**
 * Building the album category tree from a published manifest.
 *
 * Its own module because it is pure: no React, no fetch, no DOM. That is what
 * makes it testable on its own, and the three failure modes below are exactly
 * the kind that are hard to reproduce through the UI and easy to assert here.
 *
 * See documentation/specs/album-gallery-enhancement-spec.md section 8.
 */

// The album ROOT IS NOT IN THIS LIST. parent_id null means "directly under the
// album", and the root is labelled with the album's own name.
export type RawCategory = {
  id: number
  parent_id: number | null
  name: string
  position: number
}

/** The minimum a section must expose to be placed in the tree. */
export type PlaceableSection = {
  id: string
  sourceId: number
  position: number
  categoryId: number | null
}

export type CategoryNode<S extends PlaceableSection> = {
  id: string
  /** null for the root, which is the album itself rather than a category. */
  sourceId: number | null
  title: string
  depth: number
  children: CategoryNode<S>[]
  sections: S[]
  /** Everything beneath this node, itself included. */
  descendantSectionCount: number
}

/**
 * Turn a flat categories[] plus each section's categoryId into a tree.
 *
 * Three things a published manifest can contain that this must survive, because
 * losing photos to any of them would be worse than showing them in the wrong
 * place:
 *
 *   a categoryId naming a category that is not here -> that section goes to the root
 *   a parent_id naming a category that is not here  -> that category goes to the root
 *   a cycle of categories reachable from no root    -> those categories go to the root
 *
 * The last is why this walks DOWN from the root and re-attaches whatever it
 * never reached, rather than walking up from each node and trusting the chain.
 * Nothing is ever dropped.
 *
 * An empty `categories` - an older manifest, or an album nobody has organised -
 * yields a single root node holding every section in publish order, which is
 * what this app rendered before categories existed.
 */
export function buildCategoryTree<S extends PlaceableSection>(
  idPrefix: string,
  albumName: string,
  categories: RawCategory[],
  sections: S[],
): CategoryNode<S> {
  const known = new Set(categories.map((category) => category.id))

  // A parent this manifest does not contain is not a parent.
  const childrenOf = new Map<number | null, RawCategory[]>()
  const attach = (parent: number | null, category: RawCategory) => {
    const bucket = childrenOf.get(parent)
    if (bucket) bucket.push(category)
    else childrenOf.set(parent, [category])
  }
  for (const category of categories) {
    attach(
      category.parent_id != null && known.has(category.parent_id) ? category.parent_id : null,
      category,
    )
  }

  // Walk down from the root; anything never reached is in a cycle.
  const reached = new Set<number>()
  const queue: (number | null)[] = [null]
  while (queue.length > 0) {
    const current = queue.shift() as number | null
    for (const child of childrenOf.get(current) ?? []) {
      if (reached.has(child.id)) continue
      reached.add(child.id)
      queue.push(child.id)
    }
  }
  for (const category of categories) {
    if (reached.has(category.id)) continue
    const oldParent =
      category.parent_id != null && known.has(category.parent_id) ? category.parent_id : null
    if (oldParent !== null) {
      childrenOf.set(
        oldParent,
        (childrenOf.get(oldParent) ?? []).filter((c) => c.id !== category.id),
      )
    }
    attach(null, category)
    reached.add(category.id)
  }

  const sectionsOf = new Map<number | null, S[]>()
  for (const section of sections) {
    const key =
      section.categoryId != null && known.has(section.categoryId) ? section.categoryId : null
    const bucket = sectionsOf.get(key)
    if (bucket) bucket.push(section)
    else sectionsOf.set(key, [section])
  }

  // position, then id. Positions carry no uniqueness guarantee, and a stable
  // tiebreak is what stops two siblings swapping places between two loads.
  const build = (category: RawCategory | null, depth: number): CategoryNode<S> => {
    const id = category === null ? null : category.id
    const children = [...(childrenOf.get(id) ?? [])]
      .sort((left, right) => left.position - right.position || left.id - right.id)
      .map((child) => build(child, depth + 1))
    const own = [...(sectionsOf.get(id) ?? [])].sort(
      (left, right) => left.position - right.position || left.sourceId - right.sourceId,
    )
    return {
      id: category === null ? `${idPrefix}-root` : `${idPrefix}-cat-${category.id}`,
      sourceId: id,
      title: category === null ? albumName : category.name,
      depth,
      children,
      sections: own,
      descendantSectionCount:
        own.length + children.reduce((sum, child) => sum + child.descendantSectionCount, 0),
    }
  }

  return build(null, 0)
}
