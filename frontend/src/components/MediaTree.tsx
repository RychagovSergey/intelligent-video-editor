import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { Folder } from '../api/types'

interface NodeProps {
  folder: Folder
  depth: number
  selectedId: number | null
  onSelect: (folder: Folder) => void
  reloadKey: number
}

function TreeNode({ folder, depth, selectedId, onSelect, reloadKey }: NodeProps) {
  const [open, setOpen] = useState(depth === 0)
  const [children, setChildren] = useState<Folder[] | null>(null)

  useEffect(() => {
    if (!open || !folder.has_children) return
    let cancelled = false
    api.folders(folder.id).then((rows) => {
      if (!cancelled) setChildren(rows)
    })
    return () => { cancelled = true }
  }, [open, folder.id, folder.has_children, reloadKey])

  return (
    <>
      <div
        className={`tree-row${selectedId === folder.id ? ' selected' : ''}`}
        style={{ paddingLeft: 8 + depth * 14 }}
        onClick={() => onSelect(folder)}
        title={folder.path}
      >
        <span
          className="tree-caret"
          onClick={(e) => { e.stopPropagation(); if (folder.has_children) setOpen(!open) }}
        >
          {folder.has_children ? (open ? '▾' : '▸') : ''}
        </span>
        <span className="tree-name">{folder.name}</span>
        {folder.file_count > 0 && <span className="tree-count">{folder.file_count}</span>}
      </div>
      {open && children?.map((child) => (
        <TreeNode
          key={child.id}
          folder={child}
          depth={depth + 1}
          selectedId={selectedId}
          onSelect={onSelect}
          reloadKey={reloadKey}
        />
      ))}
    </>
  )
}

interface Props {
  selectedId: number | null
  onSelect: (folder: Folder) => void
  reloadKey: number
}

export function MediaTree({ selectedId, onSelect, reloadKey }: Props) {
  const [roots, setRoots] = useState<Folder[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.folders()
      .then((rows) => { setRoots(rows); setError(null) })
      .catch((e: Error) => setError(e.message))
  }, [reloadKey])

  if (error) return <div className="empty error-text">{error}</div>
  if (roots.length === 0) {
    return (
      <div className="empty">
        Хранилище пусто.<br />
        Нажмите «Сканировать», чтобы проиндексировать папку из настроек.
      </div>
    )
  }

  return (
    <div className="tree">
      {roots.map((r) => (
        <TreeNode
          key={r.id}
          folder={r}
          depth={0}
          selectedId={selectedId}
          onSelect={onSelect}
          reloadKey={reloadKey}
        />
      ))}
    </div>
  )
}
