<?php

final class MMReceivableDocumentsView
{
    public static function render(array $field): ?string
    {
        if (($field['ENTITY_ID'] ?? '') !== 'CRM_35'
            || ($field['FIELD_NAME'] ?? '') !== 'UF_CRM_35_CHAINDOCUMENTS'
            || !is_string($field['VALUE'] ?? null)
        ) {
            return null;
        }

        $value = trim($field['VALUE']);
        if ($value === '' || strlen($value) > 1000000) {
            return null;
        }

        $lines = preg_split('/\R/u', $value);
        if ($lines === false || count($lines) > 2000) {
            return null;
        }

        $documents = [];
        foreach ($lines as $line) {
            if (trim($line) === '') {
                continue;
            }
            $document = self::parseDocument(trim($line));
            if ($document === null) {
                return null;
            }
            $documents[] = $document;
        }
        if (!$documents) {
            return null;
        }

        $hasEventTypes = count(array_filter($documents, static function (array $document): bool {
            return !in_array($document['type'], ['Открытый долг', 'Реализация', 'Документ'], true);
        })) > 0;
        $hasOpenDebt = count(array_filter($documents, static function (array $document): bool {
            return $document['type'] === 'Открытый долг' || $document['gross'] !== null;
        })) > 0;
        $headers = ['Номер', 'Дата', $hasOpenDebt ? 'Остаток, ₽' : 'Сумма, ₽'];
        if ($hasEventTypes) {
            array_unshift($headers, 'Вид');
        }
        $rows = [];
        $total = 0;
        $adjustments = [];
        foreach ($documents as $document) {
            $row = [$document['number'], $document['date'], self::money($document['amount'])];
            if ($hasEventTypes) {
                array_unshift($row, $document['type'] === 'Открытый долг' ? 'Реализация' : $document['type']);
            }
            $rows[] = $row;
            $total += $document['amount'];
            if (($document['closing'] ?? 0) !== 0 || ($document['returns'] ?? 0) !== 0) {
                $adjustments[] = $document;
            }
        }

        $html = '<div class="mm-receivable-documents" data-version="2026-09-09-2" data-editor-control-type="button">';
        $html .= '<style>
.mm-receivable-documents{max-width:100%;min-width:0;color:#333;font:13px/1.45 Arial,sans-serif}
.mm-receivable-documents .mm-rd-scroll{max-width:100%;overflow-x:auto}
.mm-receivable-documents table{width:100%;border-collapse:collapse;font-size:inherit}
.mm-receivable-documents th,.mm-receivable-documents td{padding:8px 10px;text-align:left;border-bottom:1px solid #e3e8eb;white-space:nowrap}
.mm-receivable-documents th{color:#64717a;font-size:12px;font-weight:600;background:#f3f6f8}
.mm-receivable-documents td:last-child,.mm-receivable-documents th:last-child{text-align:right;font-variant-numeric:tabular-nums}
.mm-receivable-documents tfoot{font-weight:600;background:#f3f6f8}
.mm-receivable-documents details{margin-top:12px}
.mm-receivable-documents summary{cursor:pointer;color:#2375a5;padding:5px 0}
.mm-receivable-documents .mm-rd-note{color:#64717a;margin:6px 0;font-size:12px}
</style>';
        $html .= self::table($headers, $rows, 'Документы задолженности', $total);
        if ($adjustments) {
            $hasReturns = count(array_filter($adjustments, static function (array $document): bool {
                return ($document['returns'] ?? 0) !== 0;
            })) > 0;
            $detailHeaders = ['Номер', 'Сумма, ₽', 'Закрыто, ₽'];
            if ($hasReturns) {
                $detailHeaders[] = 'Возвраты, ₽';
            }
            $detailHeaders[] = 'Остаток, ₽';
            $detailRows = [];
            foreach ($adjustments as $document) {
                $row = [
                    $document['number'],
                    $document['gross'] === null ? '—' : self::money($document['gross']),
                    $document['closing'] === null ? '—' : self::money(abs($document['closing'])),
                ];
                if ($hasReturns) {
                    $row[] = $document['returns'] === null ? '—' : self::money(abs($document['returns']));
                }
                $row[] = self::money($document['amount']);
                $detailRows[] = $row;
            }
            $html .= '<details><summary>Погашения и возвраты (' . count($adjustments) . ')</summary>';
            $html .= '<p class="mm-rd-note">Закрыто — уменьшение долга по документу, не обязательно отдельная оплата.</p>';
            $html .= self::table($detailHeaders, $detailRows, 'Погашения и возвраты');
            $html .= '</details>';
        }
        return $html . '</div>';
    }

    private static function parseDocument(string $line): ?array
    {
        $moneyPattern = '-?[0-9][0-9 \x{00A0}\x{202F}]*(?:[.,][0-9]{1,2})?';
        $pattern = '/^\d+\. (Открытый долг|Реализация|Документ|Возврат|Оплата|Взаимозачет|Корректировка) (.+?)'
            . '(?: от (\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}))? на (' . $moneyPattern . ') руб\.'
            . '(?: \((.*)\))?$/u';
        if (preg_match($pattern, $line, $matches) !== 1) {
            return null;
        }
        $documentDate = $matches[3] ?? '';
        if ($documentDate !== '') {
            $parsedDate = DateTimeImmutable::createFromFormat('!d.m.Y H:i', $documentDate);
            if (!$parsedDate || $parsedDate->format('d.m.Y H:i') !== $documentDate) {
                return null;
            }
        }
        $amount = self::cents($matches[4]);
        if ($amount === null) {
            return null;
        }
        $document = [
            'type' => $matches[1],
            'number' => $matches[2],
            'date' => $documentDate === '' ? '—' : substr($documentDate, 0, 10),
            'amount' => $amount,
            'gross' => null,
            'closing' => null,
            'returns' => null,
        ];
        $detailKeys = ['исходно' => 'gross', 'закрытия' => 'closing', 'возвраты' => 'returns'];
        foreach (explode('; ', $matches[5] ?? '') as $detail) {
            if ($detail === '' || str_starts_with($detail, 'правило: ')) {
                continue;
            }
            if (preg_match('/^(исходно|закрытия|возвраты) (' . $moneyPattern . ') руб\.$/u', $detail, $parts) !== 1) {
                return null;
            }
            $detailAmount = self::cents($parts[2]);
            $key = $detailKeys[$parts[1]];
            if ($detailAmount === null || $document[$key] !== null) {
                return null;
            }
            $document[$key] = $detailAmount;
        }
        return $document;
    }

    private static function cents(string $value): ?int
    {
        $normalized = str_replace([' ', "\u{00A0}", "\u{202F}", ','], ['', '', '', '.'], $value);
        if (preg_match('/^(-?)(\d{1,12})(?:\.(\d{1,2}))?$/D', $normalized, $matches) !== 1) {
            return null;
        }
        $amount = (int)$matches[2] * 100 + (int)str_pad($matches[3] ?? '', 2, '0');
        return $matches[1] === '-' ? -$amount : $amount;
    }

    private static function money(int $cents): string
    {
        $digits = (string)intdiv(abs($cents), 100);
        $rubles = preg_replace('/\B(?=(\d{3})+(?!\d))/', "\u{202F}", $digits);
        return ($cents < 0 ? '−' : '') . $rubles . ',' . str_pad((string)(abs($cents) % 100), 2, '0', STR_PAD_LEFT);
    }

    private static function escape(string $value): string
    {
        return htmlspecialchars($value, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
    }

    private static function table(array $headers, array $rows, string $label, ?int $total = null): string
    {
        $html = '<div class="mm-rd-scroll" tabindex="0" role="region" aria-label="' . self::escape($label) . '">'
            . '<table aria-label="' . self::escape($label) . '"><thead><tr>';
        foreach ($headers as $header) {
            $html .= '<th scope="col">' . self::escape($header) . '</th>';
        }
        $html .= '</tr></thead><tbody>';
        foreach ($rows as $row) {
            $html .= '<tr>';
            foreach ($row as $cell) {
                $html .= '<td>' . self::escape($cell) . '</td>';
            }
            $html .= '</tr>';
        }
        $html .= '</tbody>';
        if ($total !== null) {
            $html .= '<tfoot><tr><td colspan="' . (count($headers) - 1) . '">По документам</td>'
                . '<td>' . self::escape(self::money($total)) . '</td></tr></tfoot>';
        }
        return $html . '</table></div>';
    }
}

if (class_exists(\Bitrix\Main\EventManager::class)) {
    \Bitrix\Main\EventManager::getInstance()->addEventHandler(
        'main',
        'onGetPublicView',
        static function (\Bitrix\Main\Event $event): ?\Bitrix\Main\EventResult {
            $parameters = $event->getParameters();
            $html = MMReceivableDocumentsView::render($parameters[0] ?? []);
            return $html === null ? null : new \Bitrix\Main\EventResult(\Bitrix\Main\EventResult::SUCCESS, $html);
        }
    );
}
