from appeal_identity import extract_identity, group_items


def test_caption_extraction_handles_consolidation_and_circuit_namespace():
    caption = ['UNITED STATES COURT OF APPEALS FOR THE NINTH CIRCUIT\n'
               'Case A        Nos. 25-3727\n'
               'capacity as Governor       25-7781\n'
               'D.C. No. 3:25-cv-04870\n'
               'Appeal from the United States District Court\n'
               'Prior appeal No. 19-1111']
    first = extract_identity('https://www.ca9.uscourts.gov/opinions/a.pdf', caption)
    assert first['dockets'] == ['25-3727', '25-7781']
    assert first['court'] == 'ca9'
    other = extract_identity('https://www.ca10.uscourts.gov/opinions/b.pdf',
                             ['Court of Appeals\nNo. 25-3727\nBefore the panel'])
    assert other['court'] == 'ca10'
    assert extract_identity('https://district.example/opinion.pdf', caption) is None


def test_etal_groups_by_explicit_docket_only():
    rows = [dict(item_id=n, raw_url=f'https://ca7.uscourts.gov/{n}.pdf',
                 headline=str(n), published_at=n, first_seen_at=n, status='NEW',
                 screening_decision='PASS') for n in (1, 2, 3)]
    identities = {1: dict(court='ca7', dockets=['20-2048']),
                  2: dict(court='ca7', dockets=['20-2048']),
                  3: dict(court='ca7', dockets=['20-2049'])}
    grouped = group_items(rows, identities, set())
    assert len(grouped) == 2
    assert {d['item_id'] for d in grouped[0]['appeal_documents']} == {1, 2}


def test_later_appeal_and_sticky_editorial_state():
    rows = [dict(item_id=n, raw_url=f'https://ca11.uscourts.gov/{n}.pdf',
                 headline=str(n), published_at=n, first_seen_at=n, status=status,
                 screening_decision='PASS') for n, status in
            [(1, 'DISMISSED'), (2, 'NEW'), (3, 'NEW')]]
    identities = {n: dict(court='ca11', dockets=[docket]) for n, docket in
                  [(1, '24-10139'), (2, '24-10139'), (3, '26-12345')]}
    grouped = group_items(rows, identities, set())
    assert len(grouped) == 2
    assert grouped[0]['item_id'] == 1
    assert grouped[1]['item_id'] == 3
    assert len(group_items(rows, identities, {1})) == 1
