package com.acme.util;

import com.acme.core.Engine;

public class Dates {
    public static final String PREFIX = "d:";

    public String today() {
        return PREFIX + "today";
    }

    public static String format(String s) {
        return PREFIX + s;
    }

    public int usesEngine(Engine e) {
        Runnable r = new Runnable() {
            @Override
            public void run() {
                e.legacy("x");
            }
        };
        r.run();
        return 1;
    }
}
