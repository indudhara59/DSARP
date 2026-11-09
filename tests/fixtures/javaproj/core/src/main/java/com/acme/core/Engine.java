package com.acme.core;

import java.util.List;
import com.acme.util.Dates;
import com.acme.util.Unused;

/** Uses Dates, which uses Engine back: a package cycle core <-> util. */
public class Engine implements Service {
    private final Dates dates = new Dates();
    protected int runs;

    @Override
    public String run(List<String> input) {
        runs++;
        return dates.today() + input.size();
    }

    /** @deprecated use {@link #run(List)} */
    @Deprecated
    public String legacy(String s) {
        return Dates.format(s);
    }
}
