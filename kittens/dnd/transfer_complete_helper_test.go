package dnd

// mark_transfer_complete is the only place the dnd tests touch the
// transfer_completed field, so the same test file can be compiled against trees
// that do not have it (a no-op version of this file is used for fix12).
func mark_transfer_complete(item *uri_list_item, complete bool) {
	item.transfer_completed = complete
}
