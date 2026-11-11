#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 图形界面版 RIX → ROL 转换器。"""

import math
import os
import queue
import threading
import traceback
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import rix2rol as converter


class Rix2RolWindow:
    def __init__(self, root):
        self.root = root
        self.root.title('RIX → ROL 转换器')
        self.root.geometry('780x670')
        self.root.minsize(680, 560)

        self.input_path = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.output_mode = tk.StringVar(value='external')
        self.output_values = {'external': '', 'subfolder': ''}
        self.active_output_mode = 'external'
        self.max_ticks = tk.StringVar(value=str(converter.DEFAULT_MAX_TICKS))
        self.tick_ms = tk.StringVar()
        self.bpm = tk.StringVar()
        self.tick_beat = tk.StringVar()
        self.beat_measure = tk.StringVar(value='4')
        self.scale_x = tk.StringVar(value='56')
        self.scale_y = tk.StringVar(value='48')
        self.use_default_options = tk.BooleanVar(value=False)
        self.export_log = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value='请选择一个 RIX 文件。')
        self.result_queue = queue.Queue()
        self.last_output_dir = ''
        self.option_widgets = []

        self._build_widgets()

    def _build_widgets(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        files = ttk.LabelFrame(self.root, text='文件')
        files.grid(row=0, column=0, padx=12, pady=(12, 6), sticky='ew')
        files.columnconfigure(1, weight=1)

        ttk.Label(files, text='输入 RIX：').grid(row=0, column=0, padx=8, pady=8)
        ttk.Entry(files, textvariable=self.input_path).grid(
            row=0, column=1, padx=4, pady=8, sticky='ew')
        ttk.Button(files, text='浏览…', command=self._browse_input).grid(
            row=0, column=2, padx=8, pady=8)

        ttk.Label(files, text='保存位置：').grid(row=1, column=0, padx=8, pady=8)
        self.output_entry = ttk.Entry(files, textvariable=self.output_dir)
        self.output_entry.grid(
            row=1, column=1, padx=4, pady=8, sticky='ew')
        self.output_browse_button = ttk.Button(
            files, text='浏览…', command=self._browse_output)
        self.output_browse_button.grid(
            row=1, column=2, padx=8, pady=8)
        output_modes = ttk.Frame(files)
        output_modes.grid(row=2, column=1, columnspan=2, padx=4, sticky='w')
        for value, label in (
            ('external', '选择外部目录'),
            ('root', '在根目录下保存'),
            ('subfolder', '在根目录下的文件夹保存'),
        ):
            ttk.Radiobutton(
                output_modes,
                text=label,
                value=value,
                variable=self.output_mode,
                command=self._update_output_controls,
            ).pack(side='left', padx=(0, 12))
        ttk.Label(
            files,
            text='根目录指 RIX 文件所在目录；外部目录/子文件夹留空时自动输出到源文件旁的 <文件名>_ROL 文件夹',
            foreground='#666666',
            wraplength=650,
        ).grid(row=3, column=1, columnspan=2, padx=4, pady=(2, 8), sticky='w')
        self._update_output_controls()

        options = ttk.LabelFrame(self.root, text='转换参数')
        options.grid(row=1, column=0, padx=12, pady=6, sticky='ew')
        for column in range(6):
            options.columnconfigure(column, weight=1)

        ttk.Checkbutton(
            options,
            text='按照默认参数转换（忽略下方所有参数）',
            variable=self.use_default_options,
            command=self._update_option_controls,
        ).grid(row=0, column=0, columnspan=6, padx=8, pady=(8, 0), sticky='w')
        self._add_option(options, '时间轴上限（tick）', self.max_ticks, 1, 0)
        self._add_option(options, '强制每 tick 毫秒数', self.tick_ms, 1, 2)
        self._add_option(options, 'BPM（需与每拍 tick 数一起设置）', self.bpm, 2, 0)
        self._add_option(options, '每拍 tick 数', self.tick_beat, 2, 2)
        self._add_option(options, '每小节拍数', self.beat_measure, 3, 0)
        self._add_option(options, '编辑缩放 X', self.scale_x, 3, 2)
        self._add_option(options, '编辑缩放 Y', self.scale_y, 3, 4)
        self._update_option_controls()

        output = ttk.LabelFrame(self.root, text='运行信息')
        output.grid(row=2, column=0, padx=12, pady=6, sticky='nsew')
        output.columnconfigure(0, weight=1)
        output.rowconfigure(0, weight=1)

        self.log = tk.Text(output, wrap='word', state='disabled', height=12)
        scrollbar = ttk.Scrollbar(output, orient='vertical', command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.grid(row=0, column=0, sticky='nsew')
        scrollbar.grid(row=0, column=1, sticky='ns')
        ttk.Checkbutton(
            output,
            text='导出日志为 TXT（保存到输出目录）',
            variable=self.export_log,
        ).grid(row=1, column=0, padx=4, pady=(6, 2), sticky='w')

        footer = ttk.Frame(self.root)
        footer.grid(row=3, column=0, padx=12, pady=(6, 12), sticky='ew')
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.status).grid(
            row=0, column=0, sticky='w')
        self.open_button = ttk.Button(
            footer, text='打开输出目录', command=self._open_output, state='disabled')
        self.open_button.grid(row=0, column=1, padx=(8, 0))
        ttk.Button(footer, text='清空日志', command=self._clear_log).grid(
            row=0, column=2, padx=(8, 0))
        self.convert_button = ttk.Button(
            footer, text='开始转换', command=self._start_conversion)
        self.convert_button.grid(row=0, column=3, padx=(8, 0))

    def _add_option(self, parent, label, variable, row, column):
        label_widget = ttk.Label(parent, text=label)
        label_widget.grid(
            row=row * 2, column=column, padx=8, pady=(8, 2), sticky='w')
        entry_widget = ttk.Entry(parent, textvariable=variable)
        entry_widget.grid(
            row=row * 2 + 1, column=column, padx=8, pady=(0, 8), sticky='ew')
        self.option_widgets.extend((label_widget, entry_widget))

    def _update_output_controls(self):
        previous_mode = self.active_output_mode
        mode = self.output_mode.get()
        if previous_mode in self.output_values:
            self.output_values[previous_mode] = self.output_dir.get()
        self.output_dir.set(self.output_values.get(mode, ''))
        self.active_output_mode = mode
        entry_state = 'disabled' if mode == 'root' else 'normal'
        browse_state = 'normal' if mode == 'external' else 'disabled'
        self.output_entry.configure(state=entry_state)
        self.output_browse_button.configure(state=browse_state)

    def _update_option_controls(self):
        state = 'disabled' if self.use_default_options.get() else 'normal'
        for widget in self.option_widgets:
            widget.configure(state=state)

    def _browse_input(self):
        path = filedialog.askopenfilename(
            title='选择 RIX 文件',
            filetypes=[('RIX 文件', '*.rix *.RIX'), ('所有文件', '*.*')],
        )
        if path:
            self.input_path.set(path)

    def _browse_output(self):
        path = filedialog.askdirectory(title='选择输出目录')
        if path:
            self.output_dir.set(path)

    def _read_options(self):
        if self.use_default_options.get():
            return None

        try:
            max_ticks = int(self.max_ticks.get())
            beat_measure = int(self.beat_measure.get())
            scale_x = int(self.scale_x.get())
            scale_y = int(self.scale_y.get())
            tick_beat = int(self.tick_beat.get()) if self.tick_beat.get().strip() else None
            tick_ms = float(self.tick_ms.get()) if self.tick_ms.get().strip() else None
            bpm = float(self.bpm.get()) if self.bpm.get().strip() else None
        except ValueError as exc:
            raise ValueError('参数必须填写为有效数字。') from exc

        if not 1 <= max_ticks <= converter.MAX_U16_TICKS:
            raise ValueError(f'时间轴上限必须在 1 到 {converter.MAX_U16_TICKS} 之间。')
        if not 1 <= beat_measure <= 65535:
            raise ValueError('每小节拍数必须在 1 到 65535 之间。')
        if not 1 <= scale_x <= 65535 or not 1 <= scale_y <= 65535:
            raise ValueError('编辑缩放值必须在 1 到 65535 之间。')
        if tick_beat is not None and not 1 <= tick_beat <= 65535:
            raise ValueError('每拍 tick 数必须在 1 到 65535 之间。')
        if tick_ms is not None and (not math.isfinite(tick_ms) or tick_ms <= 0):
            raise ValueError('每 tick 毫秒数必须是大于 0 的有限数值。')
        if bpm is not None and (not math.isfinite(bpm) or bpm <= 0):
            raise ValueError('BPM 必须是大于 0 的有限数值。')
        if (bpm is None) != (tick_beat is None):
            raise ValueError('指定 BPM 时也必须指定每拍 tick 数，反之亦然。')
        return max_ticks, tick_ms, bpm, tick_beat, beat_measure, scale_x, scale_y

    def _start_conversion(self):
        source = self.input_path.get().strip()
        if not source:
            messagebox.showerror('缺少输入文件', '请先选择一个 RIX 文件。', parent=self.root)
            return
        if not source.lower().endswith('.rix') or not os.path.isfile(source):
            messagebox.showerror('输入文件无效', '请选择存在的 .RIX 文件。', parent=self.root)
            return

        try:
            output_dir = self._resolve_output_dir(source)
        except ValueError as exc:
            messagebox.showerror('保存位置无效', str(exc), parent=self.root)
            return

        try:
            options = self._read_options()
        except ValueError as exc:
            messagebox.showerror('参数无效', str(exc), parent=self.root)
            return

        self.last_output_dir = output_dir or os.path.splitext(
            os.path.abspath(source))[0] + '_ROL'
        export_log = self.export_log.get()
        self.open_button.configure(state='disabled')
        self.convert_button.configure(state='disabled')
        self.status.set('正在转换…')
        self._append_log(f'开始处理：{source}\n')
        threading.Thread(
            target=self._convert_worker,
            args=(source, output_dir, options, export_log, self.last_output_dir),
            daemon=True,
        ).start()
        self.root.after(100, self._poll_result)

    def _resolve_output_dir(self, source):
        mode = self.output_mode.get()
        source_root = os.path.dirname(os.path.abspath(source))
        value = self.output_dir.get().strip()
        if mode == 'root':
            return source_root
        if mode == 'subfolder' and value:
            if os.path.isabs(value):
                raise ValueError('根目录下的文件夹名称不能是绝对路径。')
            output_dir = os.path.abspath(os.path.join(source_root, value))
            try:
                is_within_root = os.path.commonpath(
                    (source_root, output_dir)) == source_root
            except ValueError:
                is_within_root = False
            if not is_within_root or output_dir == source_root:
                raise ValueError('文件夹必须位于源文件根目录之下。')
            return output_dir
        if mode == 'external' and value:
            return value
        return None

    def _convert_worker(self, source, output_dir, options, export_log, log_dir):
        args = [source]
        if options is not None:
            max_ticks, tick_ms, bpm, tick_beat, beat_measure, scale_x, scale_y = options
            args.extend([
                '--max-ticks', str(max_ticks),
                '--beat-measure', str(beat_measure),
                '--scale-x', str(scale_x),
                '--scale-y', str(scale_y),
            ])
            if tick_ms is not None:
                args.extend(['--tick-ms', str(tick_ms)])
            if bpm is not None:
                args.extend(['--bpm', str(bpm), '--tick-beat', str(tick_beat)])
        if output_dir:
            args.extend(['--out', output_dir])

        stdout, stderr = StringIO(), StringIO()
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = converter.main(args)
        except Exception:
            result = 1
            stderr.write(traceback.format_exc())

        output = stdout.getvalue()
        errors = stderr.getvalue()
        log_path = ''
        log_error = ''
        if export_log:
            log_path = os.path.join(
                log_dir, os.path.splitext(os.path.basename(source))[0] + '_conversion_log.txt')
            try:
                os.makedirs(log_dir, exist_ok=True)
                log_content = output
                if errors:
                    if log_content and not log_content.endswith('\n'):
                        log_content += '\n'
                    log_content += errors
                with open(log_path, 'w', encoding='utf-8') as log_file:
                    log_file.write(log_content)
            except OSError as exc:
                log_error = f'日志导出失败：{exc}'
        self.result_queue.put((result, output, errors, log_path, log_error))

    def _poll_result(self):
        try:
            result, output, errors, log_path, log_error = self.result_queue.get_nowait()
        except queue.Empty:
            self.root.after(100, self._poll_result)
            return

        if output:
            self._append_log(output)
        if errors:
            self._append_log(errors)
        if log_path and not log_error:
            self._append_log(f'\n日志已导出：{log_path}\n')
        if log_error:
            self._append_log(f'\n{log_error}\n')
        self.convert_button.configure(state='normal')

        if result == 0:
            self.status.set('转换完成。')
            self.open_button.configure(state='normal')
            if log_error:
                messagebox.showwarning(
                    '转换完成，但日志导出失败',
                    f'ROL 和 INS 文件已生成到：\n{self.last_output_dir}\n\n{log_error}',
                    parent=self.root,
                )
            else:
                messagebox.showinfo(
                    '转换完成',
                    f'ROL 和 INS 文件已生成到：\n{self.last_output_dir}',
                    parent=self.root,
                )
        else:
            self.status.set(f'转换失败（返回码 {result}）。')
            messagebox.showerror(
                '转换失败',
                f'转换未成功，请查看运行信息。\n返回码：{result}',
                parent=self.root,
            )

    def _append_log(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', text)
        self.log.see('end')
        self.log.configure(state='disabled')

    def _clear_log(self):
        self.log.configure(state='normal')
        self.log.delete('1.0', 'end')
        self.log.configure(state='disabled')

    def _open_output(self):
        if self.last_output_dir and os.path.isdir(self.last_output_dir):
            os.startfile(self.last_output_dir)
        else:
            messagebox.showerror(
                '目录不存在', '输出目录尚未生成或已被移动。', parent=self.root)


def main():
    root = tk.Tk()
    Rix2RolWindow(root)
    root.mainloop()


if __name__ == '__main__':
    main()
