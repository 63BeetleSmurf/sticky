#!/usr/bin/python3

from gi.repository import Gdk, GLib, GObject, Gtk, Pango
from util import ends_with_url, get_url_start

TAG_DEFINITIONS = {
    'bold': {'weight': Pango.Weight.BOLD},
    'italic': {'style': Pango.Style.ITALIC},
    'underline': {'underline': Pango.Underline.SINGLE},
    'link': {'underline': Pango.Underline.SINGLE, 'foreground': 'blue'},
    'red': {'foreground': 'red'}
}

class GenericAction(object):
    def maybe_join(self, new_action):
        return False

# Used whenever plain text is added to the buffer. Internal characters such as anchor points should be handled with
# ObjectInsertAction, etc.
class AdditionAction(GenericAction):
    def __init__(self, buffer, text, location):
        super(AdditionAction, self).__init__()
        self.buffer = buffer
        self.text = text

        self.position = location.get_offset()

    def undo(self):
        start = self.buffer.get_iter_at_offset(self.position)
        end = self.buffer.get_iter_at_offset(self.position + len(self.text))
        self.buffer.delete(start, end)

    def redo(self):
        self.buffer.insert(self.buffer.get_iter_at_offset(self.position), self.text)

    def maybe_join(self, new_action):
        if not isinstance(new_action, AdditionAction):
            return False

        if new_action.position == self.position + len(self.text):
            self.text += new_action.text
            return True

        return False

# Used whenever text is removed from the buffer.
class DeletionAction(GenericAction):
    def __init__(self, buffer, start, end):
        super(DeletionAction, self).__init__()
        self.buffer = buffer
        self.text = buffer.get_slice(start, end, True)

        self.position = start.get_offset()

        if buffer.get_has_selection():
            (buffer_start, buffer_end) = buffer.get_selection_bounds()
            if buffer_start.compare(start) == 0 and buffer_end.compare(end):
                self.deletion_type = 'selection'
            else:
                self.deletion_type = 'other'
        elif self.buffer.get_iter_at_mark(self.buffer.get_insert()).compare(end) == 0:
            self.deletion_type = 'backward'
        elif self.buffer.get_iter_at_mark(self.buffer.get_insert()).compare(start) == 0:
            self.deletion_type = 'foreward'
        else:
            self.deletion_type = 'other'

    def undo(self):
        self.buffer.insert(self.buffer.get_iter_at_offset(self.position), self.text)

    def redo(self):
        start = self.buffer.get_iter_at_offset(self.position)
        end = self.buffer.get_iter_at_offset(self.position + len(self.text))
        self.buffer.delete(start, end)

    def maybe_join(self, new_action):
        if not isinstance(new_action, DeletionAction) or new_action.deletion_type != self.deletion_type:
            return False

        if self.deletion_type == 'foreward' and new_action.position == self.position:
            self.text += new_action.text
            return True
        elif self.deletion_type == 'backward' and new_action.position == self.position - 1:
            self.text = new_action.text + self.text
            self.position = new_action.position
            return True

        return False

# Used for objects inserted at an anchor point such as checkbuttons, bullets, etc.
class ObjectInsertAction(GenericAction):
    def __init__(self, buffer, anchor, is_addition=True):
        super(ObjectInsertAction, self).__init__()
        self.buffer = buffer
        self.is_addition = is_addition
        if isinstance(anchor.get_widgets()[0], Gtk.CheckButton):
            self.anchor_type = 'check'
        elif isinstance(anchor.get_widgets()[0], Gtk.Image):
            self.anchor_type = 'bullet'

        self.position = buffer.get_iter_at_child_anchor(anchor).get_offset()

    def remove(self):
        start_anchor_iter = self.buffer.get_iter_at_offset(self.position)
        end_anchor_iter = self.buffer.get_iter_at_offset(self.position + 1)
        if self.anchor_type == 'check':
            self.checked = start_anchor_iter.get_child_anchor().get_widgets()[0].get_active()
        self.buffer.delete(start_anchor_iter, end_anchor_iter)

    def add(self):
        if self.anchor_type == 'check':
            self.buffer.add_check_button(self.buffer.get_iter_at_offset(self.position), checked=self.checked)
        elif self.anchor_type == 'bullet':
            self.buffer.add_bullet(self.buffer.get_iter_at_offset(self.position))

    def undo(self):
        if self.is_addition:
            self.remove()
        else:
            self.add()

    def redo(self):
        if self.is_addition:
            self.add()
        else:
            self.remove()

# Used for setting formatting tags
class TagAction(GenericAction):
    def __init__(self, buffer, name, start, end, is_addition=True):
        super(TagAction, self).__init__()
        self.buffer = buffer
        self.name = name
        self.start = start.get_offset()
        self.end = end.get_offset()
        self.is_addition = is_addition

    def remove(self):
        self.buffer.remove_tag_by_name(self.name, self.buffer.get_iter_at_offset(self.start), self.buffer.get_iter_at_offset(self.end))

    def add(self):
        self.buffer.apply_tag_by_name(self.name, self.buffer.get_iter_at_offset(self.start), self.buffer.get_iter_at_offset(self.end))

    def undo(self):
        if self.is_addition:
            self.remove()
        else:
            self.add()

    def redo(self):
        if self.is_addition:
            self.add()
        else:
            self.remove()

# Used to combine multiple actions into one single undable action. Actions should be passed in the same order in which
# they were performed. Failure to do so could result in order getting mixed up in the buffer.
class CompositeAction(GenericAction):
    def __init__(self, *args):
        super(CompositeAction, self).__init__()
        self.child_actions = args

    def undo(self):
        for action in reversed(self.child_actions):
            action.undo()

    def redo(self):
        for action in self.child_actions:
            action.redo()

class NoteBuffer(Gtk.TextBuffer):
    # These values should not be modified directly.
    # internal_action_count keeps the "content-changed" signal from firing while the buffer performs several actions. It
    # should not be modified directly. Instead use
    #       with self.internal_action():
    #           do_something()
    internal_action_count = 0

    # in_composite and composite_actions will rarely be used in practice as it is generally much easier and
    # straightforward to construct the composite action directly as you perform them. This functionality is primarily
    # only for functions internal to the view and buffer.
    in_composite = 0
    composite_actions = []

    # used to keep track of undo and redo actions. Use self.add_undo_action() when creating a new action
    undo_actions = []
    redo_actions = []

    @GObject.Property
    def can_undo(self):
        return len(self.undo_actions)

    @GObject.Property
    def can_redo(self):
        return len(self.redo_actions)

    @GObject.Signal(flags=GObject.SignalFlags.RUN_LAST, return_type=bool,
                    accumulator=GObject.signal_accumulator_true_handled)
    def content_changed(self):
        pass

    def __init__(self):
        super(NoteBuffer, self).__init__()

        for name, attributes in TAG_DEFINITIONS.items():
            self.create_tag(name, **attributes)

        self.connect('insert-text', self.on_insert)
        self.connect('delete-range', self.on_delete)
        self.connect('begin-user-action', self.begin_composite_action)
        self.connect('end-user-action', self.end_composite_action)

    def set_view(self, view):
        def track_motion(v, event):
            mouse_iter = self.view.get_iter_at_location(*self.view.window_to_buffer_coords(Gtk.TextWindowType.TEXT, event.x, event.y))[1]
            if mouse_iter.has_tag(self.get_tag_table().lookup('link')):
                self.view.props.window.set_cursor(Gdk.Cursor.new_from_name(Gdk.Display.get_default(), 'pointer'))

                return Gdk.EVENT_STOP

            return Gdk.EVENT_PROPAGATE

        def handle_click(v, event):
            if not(event.state & Gdk.ModifierType.CONTROL_MASK) or event.button != 1:
                return Gdk.EVENT_PROPAGATE

            tag = self.get_tag_table().lookup('link')
            mouse_iter = self.view.get_iter_at_location(*self.view.window_to_buffer_coords(Gtk.TextWindowType.TEXT, event.x, event.y))[1]
            if not mouse_iter.has_tag(tag):
                return Gdk.EVENT_PROPAGATE

            start_link = mouse_iter.copy()
            start_link.backward_to_tag_toggle(tag)
            end_link = mouse_iter.copy()
            end_link.forward_to_tag_toggle(tag)

            url = self.get_slice(start_link, end_link, False)
            Gtk.show_uri(None, url, event.time)

            return Gdk.EVENT_STOP

        self.view = view
        self.view.connect('motion-notify-event', track_motion)
        self.view.connect('button-press-event', handle_click)

    def trigger_changed(self, *args):
        if self.internal_action_count == 0:
            self.emit('content-changed')

    def internal_action(self, trigger_on_complete=True):
        class InternalActionHandler(object):
            def __enter__(a):
                self.internal_action_count += 1

            def __exit__(a, exc_type, exc_value, traceback):
                self.internal_action_count -= 1
                if self.internal_action_count == 0 and trigger_on_complete:
                    GLib.idle_add(self.trigger_changed)

        return InternalActionHandler()

    def get_internal_markup(self):
        (start, end) = self.get_bounds()
        current_tags = []
        text = ''

        current_iter = start.copy()
        while current_iter.compare(end) != 0:
            # if not all tags are closed, we still need to keep track of them, but leaving them in the list will
            # cause an infinite loop, so we hold on to them in unclosed_tags and re-add them after exiting the loop
            unclosed_tags = []

            # end tags
            tags = current_iter.get_toggled_tags(False)
            while len(current_tags) and len(tags):
                tag = current_tags.pop()
                name = tag.props.name

                if not name or name not in TAG_DEFINITIONS:
                    continue

                if len(tags) == 0 or tag not in tags:
                    unclosed_tags.append(tag)
                    continue

                text += '#tag:%s:' % name
                tags.remove(tag)

            current_tags += unclosed_tags

            # start tags
            tags = current_iter.get_toggled_tags(True)
            while len(tags):
                tag = tags.pop()
                name = tag.props.name

                if not name or name not in TAG_DEFINITIONS:
                    continue

                text += '#tag:%s:' % tag.props.name
                current_tags.append(tag)

            current_char = current_iter.get_char()
            if current_char == '#':
                # we need to escape '#' characters to avoid misinterpretation when we parse it later
                text += '#'
            elif current_iter.get_child_anchor() is not None:
                # object insertions (bullets and checkboxes)
                anchor_child = current_iter.get_child_anchor().get_widgets()[0]
                if isinstance(anchor_child, Gtk.CheckButton):
                    checked = anchor_child.get_active()
                    text += '#check:' + str(int(checked))
                elif isinstance(anchor_child, Gtk.Image):
                    text += '#bullet:'
            else:
                text += current_char

            current_iter.forward_char()

        # this shouldn't ever be true, but if it is, we want to know about it
        if len(current_tags):
            print('warning: tags %s were not properly closed' % str(current_tags))

        return text

    def set_from_internal_markup(self, text):
        with self.internal_action(False):
            self.set_text('')

            current_index = 0
            open_tags = {}
            while True:
                next_index = text.find('#', current_index)
                if next_index == -1:
                    self.insert(self.get_end_iter(), text[current_index:])
                    break

                self.insert(self.get_end_iter(), text[current_index:next_index])

                if text[next_index:next_index+2] == '##':
                    self.insert(self.get_end_iter(), '#')
                    current_index = next_index + 2
                elif text[next_index:next_index+6] == '#check':
                    checked = bool(int(text[next_index+7]))
                    self.add_check_button(self.get_end_iter(), checked=checked)
                    current_index = next_index + 8
                elif text[next_index:next_index+7] == '#bullet':
                    self.add_bullet(self.get_end_iter())
                    current_index = next_index + 8
                elif text[next_index:next_index+4] == '#tag':
                    end_tag_index = text.find(':', next_index+6)
                    tag_name = text[next_index+5:end_tag_index]

                    if tag_name in open_tags:
                        mark = open_tags.pop(tag_name)
                        start = self.get_iter_at_mark(mark)
                        end = self.get_end_iter()
                        self.apply_tag_by_name(tag_name, start, end)
                        self.delete_mark(mark)
                    else:
                        open_tags[tag_name] = self.create_mark(None, self.get_end_iter(), True)

                    current_index = next_index + 6 + len(tag_name)

    def undo(self, *args):
        if len(self.undo_actions) == 0:
            print('warning: attempting to undo action when there is nothing to undo')
            return

        with self.internal_action():
            action = self.undo_actions.pop()
            action.undo()
            self.redo_actions.append(action)

    def redo(self, *args):
        if len(self.redo_actions) == 0:
            print('warning: attempting to redo action when there is nothing to redo')
            return

        with self.internal_action():
            action = self.redo_actions.pop()
            action.redo()
            self.undo_actions.append(action)

    def begin_composite_action(self, *args):
        self.in_composite += 1

    def end_composite_action(self, *args):
        self.in_composite -= 1

        # if there are no actions that happen during the composite, there's nothing we need to do
        if self.in_composite or len(self.composite_actions) == 0:
            return

        # some times we get actions tagged as composite when they really shouldn't be, so if there's just one action
        # we don't want to put it inside a composite action as that will break joining actions if applicable
        if len(self.composite_actions) == 1:
            self.add_undo_action(self.composite_actions[0])
        else:
            self.add_undo_action(CompositeAction(*self.composite_actions))

        self.composite_actions.clear()

    def add_undo_action(self, action):
        if self.in_composite:
            self.composite_actions.append(action)
        else:
            self.undo_actions.append(action)
            self.redo_actions.clear()

    def on_insert(self, buffer, location, text, *args):
        if self.internal_action_count:
            return

        with self.internal_action():
            compound = False
            action = AdditionAction(self, text, location)

            if text in ['\n', '\t', ' ', '.', ',', ';', ':']:
                pre_text = self.get_slice(self.get_start_iter(), location, True)
                match = get_url_start(pre_text)
                if match:
                    self.add_undo_action(self.add_tag('link', self.get_iter_at_offset(match.start()), location))

            if text == '\n' and self.maybe_repeat(location, action):
                compound = True

            if not compound and (not self.props.can_undo or not self.undo_actions[-1].maybe_join(action)):
                self.add_undo_action(action)

    def on_delete(self, buffer, start, end):
        if self.internal_action_count:
            return

        # if there were tags, bullets or checkboxes here, we need to handle those first so that we can undo later
        actions = []
        with self.internal_action():
            current_iter = start.copy()

            start_mark = Gtk.TextMark()
            end_mark = Gtk.TextMark()

            self.add_mark(start_mark, start)
            self.add_mark(end_mark, end)

            open_tags = {}
            while current_iter.compare(end) < 0:
                anchor = current_iter.get_child_anchor()
                if anchor is not None:
                    current_offset = current_iter.get_offset()
                    action = ObjectInsertAction(self, anchor, is_addition=False)
                    actions.append(action)
                    action.remove()

                    current_iter = self.get_iter_at_offset(current_offset)
                    end.assign(self.get_iter_at_mark(end_mark))
                    start.assign(self.get_iter_at_mark(start_mark))

                for tag in current_iter.get_toggled_tags(True):
                    # ignore tags that don't have one of our names (i.e. spell checker)
                    if tag.props.name in TAG_DEFINITIONS:
                        open_tags[tag.props.name] = current_iter.get_offset()

                for tag in current_iter.get_toggled_tags(False):
                    # ignore tags that don't have one of our names (i.e. spell checker)
                    if tag.props.name not in TAG_DEFINITIONS:
                        continue

                    if tag.props.name in open_tags:
                        actions.append(TagAction(self, tag.props.name, self.get_iter_at_offset(open_tags[tag.props.name]), current_iter, False))
                        del open_tags[tag.props.name]
                    else:
                        actions.append(TagAction(self, tag.props.name, start, current_iter, False))

                current_iter.forward_char()

            for name, offset in open_tags.items():
                actions.append(TagAction(self, name, self.get_iter_at_offset(offset), end, False))

            self.delete_mark(start_mark)
            self.delete_mark(end_mark)

            # if it's just an object deletion, there's nothing left to remove, so there's no need to create an undo action
            if start.compare(end) != 0:
                actions.append(DeletionAction(self, start, end))

            if len(actions) == 0:
                return Gdk.EVENT_STOP
            elif len(actions) == 1:
                action = actions[0]
            else:
                actions.append(action)
                action = CompositeAction(*actions)

            if self.props.can_undo and self.undo_actions[-1].maybe_join(action):
                return Gdk.EVENT_PROPAGATE

            self.add_undo_action(action)

    def tag_selection(self, tag_name):
        if self.get_has_selection():
            self.add_undo_action(self.add_tag(tag_name, *self.get_selection_bounds()))
        else:
            cursor_location = self.get_iter_at_mark(self.get_insert())
            self.add_undo_action(self.add_tag(tag_name, cursor_location, cursor_location))

    def add_tag(self, tag_name, start, end):
        self.apply_tag_by_name(tag_name, start, end)
        action = TagAction(self, tag_name, start, end)
        self.trigger_changed()

        return action

    def add_check_button(self, a_iter, checked=False):
        with self.internal_action():
            anchor = self.create_child_anchor(a_iter)
            check_button = Gtk.CheckButton(visible=True, active=checked, margin_right=5, margin_top=5)
            check_button.connect('toggled', self.trigger_changed)
            self.view.add_child_at_anchor(check_button, anchor)

            return ObjectInsertAction(self, anchor)

    def add_bullet(self, a_iter):
        with self.internal_action():
            anchor = self.create_child_anchor(a_iter)
            bullet = Gtk.Image(visible=True, icon_name='menu-bullet', pixel_size=16)
            self.view.add_child_at_anchor(bullet, anchor)

            return ObjectInsertAction(self, anchor)

    def toggle_checklist(self, *args):
        actions = []
        with self.internal_action():
            if self.get_has_selection():
                (start, end) = self.get_selection_bounds()
            else:
                start = end = self.get_iter_at_mark(self.get_insert())

            line_index_start = start.get_line()
            line_index_end = end.get_line()

            all_have_checkboxes = True
            for line in range(line_index_start, line_index_end + 1):
                if self.get_iter_at_line(line).get_child_anchor() is None:
                    all_have_checkboxes = False
                    break

            for line in range(line_index_start, line_index_end + 1):
                if all_have_checkboxes:
                    anchor = self.get_iter_at_line(line).get_child_anchor()
                    if anchor is not None:
                        action = ObjectInsertAction(self, anchor, False)
                        action.remove()
                        actions.append(action)
                else:
                    actions.append(self.add_check_button(self.get_iter_at_line(line)))

            if len(actions):
                self.add_undo_action(CompositeAction(*actions))

    def toggle_bullets(self, *args):
        actions = []
        with self.internal_action():
            if self.get_has_selection():
                (start, end) = self.get_selection_bounds()
            else:
                start = end = self.get_iter_at_mark(self.get_insert())

            line_index_start = start.get_line()
            line_index_end = end.get_line()

            all_have_bullets = True
            for line in range(line_index_start, line_index_end + 1):
                if self.get_iter_at_line(line).get_child_anchor() is None:
                    all_have_bullets = False
                    break

            for line in range(line_index_start, line_index_end + 1):
                if all_have_bullets:
                    anchor = self.get_iter_at_line(line).get_child_anchor()
                    if anchor is not None:
                        action = ObjectInsertAction(self, anchor, False)
                        action.remove()
                        actions.append(action)
                else:
                    actions.append(self.add_bullet(self.get_iter_at_line(line)))

        if len(actions):
            self.add_undo_action(CompositeAction(*actions))

    def maybe_repeat(self, current_iter, prev_action):
        line_start = current_iter.copy()
        line_start.set_line_index(0)
        anchor = line_start.get_child_anchor()

        if anchor is None:
            return False

        offset = current_iter.get_offset()
        if isinstance(anchor.get_widgets()[0], Gtk.CheckButton):
            action = self.add_check_button(current_iter)
        if isinstance(anchor.get_widgets()[0], Gtk.Image):
            action = self.add_bullet(current_iter)
        self.add_undo_action(CompositeAction(prev_action, action))

        current_iter.assign(self.get_iter_at_offset(offset))

        return True

    def test(self):
        print(ends_with_url(self.get_text(*self.get_bounds(), False)))